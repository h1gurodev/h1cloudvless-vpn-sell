"""Balance top-up handlers backed by the 2328.io crypto payment API."""
from __future__ import annotations

import logging
import time
import uuid as uuidlib

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import keyboards as kb
from ..config import Settings
from ..db import Database
from ..payments import PaymentClient, PaymentError
from ..texts import fmt_money

log = logging.getLogger("vpnsell.topup")
router = Router(name="topup")


@router.callback_query(F.data == "topup")
async def cb_topup(cb: CallbackQuery, settings: Settings) -> None:
    if not settings.pay_enabled:
        await cb.answer("Пополнение временно недоступно", show_alert=True)
        return
    await cb.message.edit_text(
        "Выбери сумму пополнения:", reply_markup=kb.topup_menu(settings)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("topup:"))
async def cb_topup_amount(
    cb: CallbackQuery, settings: Settings, db: Database, pay: PaymentClient
) -> None:
    if not settings.pay_enabled:
        await cb.answer("Пополнение недоступно", show_alert=True)
        return
    raw = cb.data.split(":", 1)[1]
    if not raw.isdigit() or int(raw) <= 0:
        await cb.answer("Некорректная сумма", show_alert=True)
        return
    amount = int(raw)

    await db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    await cb.answer("Создаю счёт…")

    # order_id is our idempotency key and ties the payment back to the user.
    order_id = f"tg{cb.from_user.id}-{int(time.time())}-{uuidlib.uuid4().hex[:8]}"
    try:
        result = await pay.create_payment(
            amount=amount,
            currency=settings.pay_currency,
            order_id=order_id,
            description=f"Пополнение баланса на {fmt_money(amount, settings.pay_currency)}",
            ttl_seconds=settings.pay_ttl_seconds,
        )
    except PaymentError as exc:
        log.warning("create_payment failed: %s", exc)
        await cb.message.edit_text(
            f"Не удалось создать счёт: {exc}\nПопробуй позже.",
            reply_markup=kb.balance_menu(settings),
        )
        return

    pay_uuid = str(result.get("uuid") or "")
    pay_url = str(result.get("url") or "")
    if not pay_uuid or not pay_url:
        await cb.message.edit_text(
            "Платёжная система не вернула ссылку. Попробуй позже.",
            reply_markup=kb.balance_menu(settings),
        )
        return

    await db.create_payment(
        pay_uuid, order_id, cb.from_user.id, amount, settings.pay_currency, pay_url
    )
    await cb.message.edit_text(
        f"Счёт на {fmt_money(amount, settings.pay_currency)} создан.\n\n"
        "Нажми «Оплатить», выбери криптовалюту и оплати. Баланс пополнится "
        "автоматически после подтверждения сети.",
        reply_markup=kb.pay_link_menu(pay_url, pay_uuid),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("checkpay:"))
async def cb_checkpay(
    cb: CallbackQuery, settings: Settings, db: Database, pay: PaymentClient
) -> None:
    pay_uuid = cb.data.split(":", 1)[1]
    record = await db.get_payment(pay_uuid)
    if not record or record["telegram_id"] != cb.from_user.id:
        await cb.answer("Счёт не найден", show_alert=True)
        return

    if record["credited"]:
        await cb.answer("Оплата уже зачислена ✅", show_alert=True)
        await _show_balance(cb, settings, db)
        return

    # Force an immediate status check instead of waiting for the next poll.
    try:
        info = await pay.payment_info(uuid=pay_uuid)
    except PaymentError as exc:
        await cb.answer(f"Не удалось проверить: {exc}", show_alert=True)
        return

    status = str(info.get("payment_status") or "")
    if status in {"paid", "overpaid"}:
        credited = await db.credit_payment_if_new(pay_uuid, status)
        if credited:
            await cb.answer("Оплата получена! Баланс пополнен ✅", show_alert=True)
        else:
            await cb.answer("Оплата уже зачислена ✅", show_alert=True)
        await _show_balance(cb, settings, db)
    else:
        await db.set_payment_status(pay_uuid, status)
        await cb.answer("Оплата ещё не поступила. Попробуй чуть позже.", show_alert=True)


async def _show_balance(cb: CallbackQuery, settings: Settings, db: Database) -> None:
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    from ..texts import balance_text

    try:
        await cb.message.edit_text(
            balance_text(user.balance, settings.currency),
            reply_markup=kb.balance_menu(settings),
        )
    except Exception:
        pass
