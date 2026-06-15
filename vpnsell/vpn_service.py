"""Provisioning service: creates/renews VLESS clients on the node and keeps
the local subscription records in sync."""
from __future__ import annotations

import logging
import time

from .config import Plan, Settings
from .db import Database, Subscription
from .h1cloud import H1CloudClient, H1CloudError, sanitize_name

log = logging.getLogger("vpnsell.vpn")


class VPNService:
    def __init__(self, settings: Settings, db: Database, client: H1CloudClient):
        self.settings = settings
        self.db = db
        self.client = client

    def _vpn_name(self, telegram_id: int, suffix: str) -> str:
        """Stable, node-safe name unique per user + slot."""
        return sanitize_name(f"tg{telegram_id}_{suffix}")

    def subscription_url(self, uuid: str | None, vpn_name: str) -> str:
        template = self.settings.sub_public_url
        if not template:
            return ""
        return template.replace("{uuid}", uuid or vpn_name)

    async def provision(
        self, telegram_id: int, plan: Plan, *, is_trial: bool = False
    ) -> Subscription:
        """Create a brand-new subscription on the node and store it locally."""
        suffix = "trial" if is_trial else plan.id
        vpn_name = self._vpn_name(telegram_id, suffix)

        # If a client with this name already lingers on the node, reuse it by
        # renewing rather than failing on user_already_exists.
        existing_node = await self.client.get_client(vpn_name)
        if existing_node is None:
            await self.client.create_client(
                vpn_name,
                plan.days,
                traffic_limit_gb=plan.traffic_gb,
                device_limit=plan.devices,
            )
        else:
            await self.client.renew_client(vpn_name, plan.days)
            await self.client.set_limits(
                vpn_name,
                traffic_limit_gb=plan.traffic_gb,
                device_limit=plan.devices,
            )

        node_client = await self.client.get_client(vpn_name)
        uuid = str(node_client.get("uuid")) if node_client else None
        expires_at = int(time.time()) + plan.days * 86400
        if node_client and node_client.get("expires_at"):
            try:
                expires_at = int(node_client["expires_at"])
            except (TypeError, ValueError):
                pass

        # If the user already holds this plan slot, extend the existing record
        # instead of inserting a duplicate (vpn_name is UNIQUE).
        existing_sub = await self.db.get_subscription_by_name(vpn_name)
        if existing_sub is not None:
            base = max(existing_sub.expires_at, int(time.time()))
            new_expiry = expires_at if existing_node is None else base + plan.days * 86400
            await self.db.update_subscription(existing_sub.id, expires_at=new_expiry, uuid=uuid)
            existing_sub.expires_at = new_expiry
            existing_sub.uuid = uuid
            log.info("Re-provisioned existing %s for user %s", vpn_name, telegram_id)
            return existing_sub

        sub = Subscription(
            id=0,
            telegram_id=telegram_id,
            vpn_name=vpn_name,
            uuid=uuid,
            plan_id=plan.id,
            expires_at=expires_at,
            traffic_gb=plan.traffic_gb,
            devices=plan.devices,
            is_trial=is_trial,
            created_at=int(time.time()),
        )
        sub.id = await self.db.add_subscription(sub)
        log.info("Provisioned %s for user %s (trial=%s)", vpn_name, telegram_id, is_trial)
        return sub

    async def renew(self, sub: Subscription, plan: Plan) -> Subscription:
        """Extend an existing subscription by plan.days."""
        existing_node = await self.client.get_client(sub.vpn_name)
        if existing_node is None:
            # Client was removed on the node; recreate from scratch.
            await self.client.create_client(
                sub.vpn_name,
                plan.days,
                traffic_limit_gb=plan.traffic_gb,
                device_limit=plan.devices,
            )
        else:
            await self.client.renew_client(sub.vpn_name, plan.days)

        node_client = await self.client.get_client(sub.vpn_name)
        uuid = str(node_client.get("uuid")) if node_client else sub.uuid
        expires_at = max(sub.expires_at, int(time.time())) + plan.days * 86400
        if node_client and node_client.get("expires_at"):
            try:
                expires_at = int(node_client["expires_at"])
            except (TypeError, ValueError):
                pass

        await self.db.update_subscription(sub.id, expires_at=expires_at, uuid=uuid)
        sub.expires_at = expires_at
        sub.uuid = uuid
        log.info("Renewed %s for user %s", sub.vpn_name, sub.telegram_id)
        return sub

    async def links_for(self, sub: Subscription) -> list[str]:
        try:
            return await self.client.get_links(sub.vpn_name)
        except H1CloudError as exc:
            log.warning("Failed to fetch links for %s: %s", sub.vpn_name, exc)
            return []
