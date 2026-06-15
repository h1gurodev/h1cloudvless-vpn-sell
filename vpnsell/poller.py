"""Background poller for 2328.io payments.

Instead of receiving webhooks, we periodically ask the provider for the status
of each uncredited payment. When one turns paid/overpaid we credit the user's
balance through the same idempotent path used everywhere else, so there's no
risk of double-crediting if the manual "check payment" button races the poller.

This needs no public URL or TLS — it only makes outbound calls.
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .payments import PaymentClient, PaymentError
from .texts import fmt_money

log = logging.getLogger("vpnsell.poller")

_PAID_STATUSES = {"paid", "overpaid"}
# Stop polling invoices older than their lifetime plus this grace window.
_EXPIRY_GRACE = 600


async def _credit_and_notify(settings: Settings, db: Database, bot, uuid: str, status: str) -> None:
    credited = await db.credit_payment_if_new(uuid, status)
    if not credited:
        return
    log.info(
        "Credited %s to user %s (payment %s)",
        credited["amount"], credited["telegram_id"], uuid,
    )
    try:
        await bot.send_message(
            credited["telegram_id"],
            f"✅ Оплата получена! Баланс пополнен на "
            f"{fmt_money(credited['amount'], credited['currency'])}.",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to notify user about top-up: %s", exc)


async def poll_once(settings: Settings, db: Database, pay: PaymentClient, bot) -> None:
    """One sweep over all still-pending payments."""
    max_age = settings.pay_ttl_seconds + _EXPIRY_GRACE
    expired = await db.expire_stale_payments(max_age)
    if expired:
        log.info("Expired %s stale payment(s)", expired)

    pending = await db.list_pending_payments(max_age_seconds=max_age)
    for record in pending:
        uuid = record["uuid"]
        try:
            info = await pay.payment_info(uuid=uuid)
        except PaymentError as exc:
            log.debug("poll payment_info failed for %s: %s", uuid, exc)
            continue
        status = str(info.get("payment_status") or "")
        if status in _PAID_STATUSES:
            await _credit_and_notify(settings, db, bot, uuid, status)
        elif status and status != record["status"]:
            await db.set_payment_status(uuid, status)


async def run_poller(settings: Settings, db: Database, pay: PaymentClient, bot) -> None:
    """Loop forever, sweeping pending payments every pay_poll_interval seconds."""
    interval = settings.pay_poll_interval
    log.info("Payment poller started (every %ss)", interval)
    while True:
        try:
            await poll_once(settings, db, pay, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.warning("Payment poll sweep failed: %s", exc)
        await asyncio.sleep(interval)
