"""Admin handlers: stats, credit balance, node health."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..config import Settings
from ..db import Database
from ..h1cloud import H1CloudClient, H1CloudError
from ..texts import fmt_money

router = Router(name="admin")


class CreditStates(StatesGroup):
    waiting_input = State()


def _is_admin(settings: Settings, user_id: int) -> bool:
    return settings.is_admin(user_id)


@router.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery, settings: Settings) -> None:
    if not _is_admin(settings, cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.message.edit_text("⚙️ Панель администратора", reply_markup=kb.admin_menu())
    await cb.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(cb: CallbackQuery, settings: Settings, db: Database) -> None:
    if not _is_admin(settings, cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    s = await db.stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: {s['users']}\n"
        f"Активных подписок: {s['active_subs']}\n"
        f"Выручка: {fmt_money(s['revenue'], settings.currency)}"
    )
    await cb.message.edit_text(text, reply_markup=kb.admin_menu())
    await cb.answer()


@router.callback_query(F.data == "admin:health")
async def cb_admin_health(cb: CallbackQuery, settings: Settings, h1: H1CloudClient) -> None:
    if not _is_admin(settings, cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.answer("Проверяю…")
    try:
        await h1.health()
        text = "🩺 Узел отвечает ✅"
    except H1CloudError as exc:
        text = f"🩺 Узел недоступен ❌\n\n{exc}"
    await cb.message.edit_text(text, reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:credit")
async def cb_admin_credit(cb: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    if not _is_admin(settings, cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return
    await state.set_state(CreditStates.waiting_input)
    await cb.message.edit_text(
        "Отправь сообщение в формате:\n\n"
        "<code>USER_ID СУММА</code>\n\n"
        "Например: <code>123456789 500</code>\n"
        "Сумма может быть отрицательной для списания.\n\n"
        "Отмена: /cancel",
        reply_markup=kb.back_home(),
    )
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        await state.clear()
        await message.answer("Отменено.")


@router.message(CreditStates.waiting_input)
async def on_credit_input(
    message: Message, settings: Settings, db: Database, state: FSMContext
) -> None:
    if not _is_admin(settings, message.from_user.id):
        await state.clear()
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Неверный формат. Нужно: USER_ID СУММА")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.answer("Сумма должна быть числом.")
        return

    target_id = int(parts[0])
    user = await db.get_user(target_id)
    if user is None:
        await message.answer("Пользователь не найден (он должен сначала написать боту).")
        return

    new_balance = await db.adjust_balance(target_id, amount)
    await db.record_order(target_id, None, amount, "topup")
    await state.clear()
    await message.answer(
        f"Готово. Баланс пользователя {target_id}: "
        f"{fmt_money(new_balance, settings.currency)}"
    )
    try:
        await message.bot.send_message(
            target_id,
            f"💰 Ваш баланс изменён на {fmt_money(amount, settings.currency)}. "
            f"Текущий баланс: {fmt_money(new_balance, settings.currency)}",
        )
    except Exception:
        pass
