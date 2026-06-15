"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Plan:
    """A purchasable tariff. traffic_gb / devices of 0 mean unlimited."""

    id: str
    title: str
    days: int
    price: int
    traffic_gb: int = 0
    devices: int = 0


def _parse_plans(raw: str) -> list[Plan]:
    plans: list[Plan] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 4:
            continue
        pid, title, days, price = parts[0], parts[1], parts[2], parts[3]
        traffic = parts[4] if len(parts) > 4 else "0"
        devices = parts[5] if len(parts) > 5 else "0"
        try:
            plans.append(
                Plan(
                    id=pid.strip(),
                    title=title.strip(),
                    days=int(days),
                    price=int(price),
                    traffic_gb=int(traffic),
                    devices=int(devices),
                )
            )
        except ValueError:
            continue
    return plans


_DEFAULT_PLANS = (
    "m1:1 месяц:30:149:0:3;"
    "m3:3 месяца:90:399:0:3;"
    "m6:6 месяцев:180:699:0:3;"
    "m12:1 год:365:1199:0:5"
)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    drop_pending_updates: bool
    db_path: str
    log_level: str

    h1_api_url: str
    h1_api_token: str
    h1_verify_ssl: bool
    h1_request_timeout: int
    h1_node_name: str

    sub_public_url: str

    currency: str
    trial_days: int
    trial_traffic_gb: int
    trial_devices: int

    # --- 2328.io crypto payments ---
    pay_enabled: bool
    pay_base_url: str
    pay_project_uuid: str
    pay_api_key: str
    pay_user_agent: str
    pay_currency: str
    pay_ttl_seconds: int
    pay_poll_interval: int
    topup_amounts: tuple[int, ...] = field(default_factory=tuple)
    plans: tuple[Plan, ...] = field(default_factory=tuple)

    def plan_by_id(self, plan_id: str) -> Plan | None:
        for plan in self.plans:
            if plan.id == plan_id:
                return plan
        return None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_settings() -> Settings:
    admin_ids = {
        int(part)
        for part in _get("ADMIN_IDS").replace(";", ",").split(",")
        if part.strip().lstrip("-").isdigit()
    }
    plans = tuple(_parse_plans(_get("PLANS") or _DEFAULT_PLANS))

    topup_amounts = tuple(
        int(part)
        for part in _get("TOPUP_AMOUNTS").replace(";", ",").split(",")
        if part.strip().isdigit()
    ) or (100, 300, 500, 1000)

    pay_api_key = _get("PAY_API_KEY")
    pay_project_uuid = _get("PAY_PROJECT_UUID")

    return Settings(
        bot_token=_get("BOT_TOKEN"),
        admin_ids=frozenset(admin_ids),
        drop_pending_updates=_get_bool("DROP_PENDING_UPDATES", True),
        db_path=_get("DB_PATH") or "./vpnsell.db",
        log_level=_get("LOG_LEVEL") or "INFO",
        h1_api_url=_get("H1_API_URL"),
        h1_api_token=_get("H1_API_TOKEN"),
        h1_verify_ssl=_get_bool("H1_VERIFY_SSL", False),
        h1_request_timeout=_get_int("H1_REQUEST_TIMEOUT", 20),
        h1_node_name=_get("H1_NODE_NAME") or "VPN",
        sub_public_url=_get("SUB_PUBLIC_URL"),
        currency=_get("CURRENCY") or "RUB",
        trial_days=_get_int("TRIAL_DAYS", 3),
        trial_traffic_gb=_get_int("TRIAL_TRAFFIC_GB", 10),
        trial_devices=_get_int("TRIAL_DEVICES", 1),
        pay_enabled=_get_bool("PAY_ENABLED", False) and bool(pay_api_key and pay_project_uuid),
        pay_base_url=(_get("PAY_BASE_URL") or "https://api.2328.io/api").rstrip("/"),
        pay_project_uuid=pay_project_uuid,
        pay_api_key=pay_api_key,
        pay_user_agent=_get("PAY_USER_AGENT") or "vpnsell/1.0 (+https://t.me)",
        pay_currency=_get("PAY_CURRENCY") or (_get("CURRENCY") or "RUB"),
        pay_ttl_seconds=_get_int("PAY_TTL_SECONDS", 3600),
        pay_poll_interval=max(10, _get_int("PAY_POLL_INTERVAL", 30)),
        topup_amounts=topup_amounts,
        plans=plans,
    )
