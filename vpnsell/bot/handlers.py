"""User-facing handlers: menu, buy, subscriptions, trial, balance, help."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import keyboards as kb
from .. import texts
from ..config import Settings
from ..db import Database
from ..qr import make_qr
from ..vpn_service import VPNService

log = logging.getLogger("vpnsell.handlers")
router = Router(name="user")


def _display_name(message_or_cb) -> str:
    user = message_or_cb.from_user
    return user.first_name or user.username or "друг"


async def _show_home(target: Message, settings: Settings, db: Database, user_id: int) -> None:
    is_admin = settings.is_admin(user_id)
    await target.answer(
        texts.welcome(_display_name(target)),
        reply_markup=kb.main_menu(settings, is_admin),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, settings: Settings, db: Database) -> None:
    # /start may carry a referral payload: "/start ref_<id>"
    referred_by = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        ref = parts[1][4:]
        if ref.isdigit() and int(ref) != message.from_user.id:
            referred_by = int(ref)
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, referred_by
    )
    await _show_home(message, settings, db, message.from_user.id)


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, settings: Settings) -> None:
    is_admin = settings.is_admin(cb.from_user.id)
    await cb.message.edit_text(
        texts.welcome(_display_name(cb)),
        reply_markup=kb.main_menu(settings, is_admin),
    )
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery) -> None:
    await cb.message.edit_text(texts.HELP_TEXT, reply_markup=kb.back_home())
    await cb.answer()


@router.callback_query(F.data == "balance")
async def cb_balance(cb: CallbackQuery, settings: Settings, db: Database) -> None:
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.username)
    await cb.message.edit_text(
        texts.balance_text(user.balance, settings.currency, settings.pay_enabled),
        reply_markup=kb.balance_menu(settings),
    )
    await cb.answer()


# --- buy flow ---

@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery, settings: Settings) -> None:
    if not settings.plans:
        await cb.answer("Тарифы не настроены", show_alert=True)
        return
    await cb.message.edit_text(
        "Выбери тариф:", reply_markup=kb.plans_menu(settings)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cb: CallbackQuery, settings: Settings) -> None:
    plan = settings.plan_by_id(cb.data.split(":", 1)[1])
    if not plan:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    text = (
        f"<b>{plan.title}</b>\n\n"
        f"Срок: {plan.days} дн.\n"
        f"Лимиты: {texts.fmt_quota(plan.traffic_gb, plan.devices)}\n"
        f"Цена: <b>{texts.fmt_money(plan.price, settings.currency)}</b>\n\n"
        "Оплата происходит с внутреннего баланса."
    )
    await cb.message.edit_text(text, reply_markup=kb.confirm_buy(plan.id))
    await cb.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, settings: Settings, db: Database, vpn: VPNService) -> None:
    plan = settings.plan_by_id(cb.data.split(":", 1)[1])
    if not plan:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    await db.get_or_create_user(cb.from_user.id, cb.from_user.username)

    # Debit atomically up front so a double-tap can't overdraw the balance:
    # only one concurrent call gets a non-None result.
    new_balance = await db.try_debit(cb.from_user.id, plan.price)
    if new_balance is None:
        user = await db.get_user(cb.from_user.id)
        await cb.message.edit_text(
            texts.out_of_funds(plan.price, user.balance if user else 0, settings.currency),
            reply_markup=kb.balance_menu(settings),
        )
        await cb.answer()
        return

    await cb.answer("Создаю подписку…")
    try:
        sub = await vpn.provision(cb.from_user.id, plan, is_trial=False)
    except Exception as exc:  # noqa: BLE001 - refund on any failure
        # Refund: the customer was charged but got nothing.
        await db.adjust_balance(cb.from_user.id, plan.price)
        log.warning("provision failed, refunded %s: %s", plan.price, exc)
        await cb.message.edit_text(
            "Не удалось создать подписку на сервере. Средства возвращены на баланс, "
            "попробуй позже или напиши администратору.",
            reply_markup=kb.back_home(),
        )
        return

    await db.record_order(cb.from_user.id, plan.id, plan.price, "purchase")

    sub_url = vpn.subscription_url(sub.uuid, sub.vpn_name)
    links = await vpn.links_for(sub)
    await cb.message.edit_text(
        "✅ Подписка активирована!\n\n"
        + texts.subscription_card(sub, links, sub_url),
        reply_markup=kb.back_home(),
        disable_web_page_preview=True,
    )


# --- trial ---

@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery, settings: Settings, db: Database, vpn: VPNService) -> None:
    if settings.trial_days <= 0:
        await cb.answer("Пробный период отключён", show_alert=True)
        return
    await db.get_or_create_user(cb.from_user.id, cb.from_user.username)

    # Claim atomically: only the first concurrent tap wins, preventing two
    # trials from a double-click.
    if not await db.claim_trial(cb.from_user.id):
        await cb.answer("Пробный период уже был использован", show_alert=True)
        return

    await cb.answer("Активирую пробный период…")
    from ..config import Plan

    trial_plan = Plan(
        id="trial",
        title="Пробный период",
        days=settings.trial_days,
        price=0,
        traffic_gb=settings.trial_traffic_gb,
        devices=settings.trial_devices,
    )
    try:
        sub = await vpn.provision(cb.from_user.id, trial_plan, is_trial=True)
    except Exception as exc:  # noqa: BLE001 - release the claim on failure
        await db.reset_trial(cb.from_user.id)
        log.warning("trial provision failed, released claim: %s", exc)
        await cb.message.edit_text(
            "Не удалось активировать пробный период. Попробуй позже.",
            reply_markup=kb.back_home(),
        )
        return

    sub_url = vpn.subscription_url(sub.uuid, sub.vpn_name)
    links = await vpn.links_for(sub)
    await cb.message.edit_text(
        "🎁 Пробный период активирован!\n\n"
        + texts.subscription_card(sub, links, sub_url),
        reply_markup=kb.back_home(),
        disable_web_page_preview=True,
    )


# --- my subscriptions ---

@router.callback_query(F.data == "subs")
async def cb_subs(cb: CallbackQuery, db: Database) -> None:
    subs = await db.list_subscriptions(cb.from_user.id)
    if not subs:
        await cb.message.edit_text(
            "У тебя пока нет подписок. Купи VPN или активируй пробный период.",
            reply_markup=kb.back_home(),
        )
        await cb.answer()
        return
    await cb.message.edit_text(
        "Твои подписки:", reply_markup=kb.subs_menu(subs)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("sub:"))
async def cb_sub_detail(cb: CallbackQuery, db: Database, vpn: VPNService) -> None:
    sub_id = int(cb.data.split(":", 1)[1])
    sub = await db.get_subscription(sub_id)
    if not sub or sub.telegram_id != cb.from_user.id:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    await cb.answer()
    sub_url = vpn.subscription_url(sub.uuid, sub.vpn_name)
    links = await vpn.links_for(sub)
    await cb.message.edit_text(
        texts.subscription_card(sub, links, sub_url),
        reply_markup=kb.sub_detail_menu(sub),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("qr:"))
async def cb_qr(cb: CallbackQuery, db: Database, vpn: VPNService) -> None:
    sub_id = int(cb.data.split(":", 1)[1])
    sub = await db.get_subscription(sub_id)
    if not sub or sub.telegram_id != cb.from_user.id:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    sub_url = vpn.subscription_url(sub.uuid, sub.vpn_name)
    if not sub_url:
        await cb.answer("Ссылка-подписка недоступна", show_alert=True)
        return
    await cb.answer()
    png = make_qr(sub_url)
    await cb.message.answer_photo(
        BufferedInputFile(png, filename="subscription.png"),
        caption="Отсканируй QR в приложении или используй ссылку-подписку.",
    )


# --- renew ---

@router.callback_query(F.data.startswith("renew:"))
async def cb_renew(cb: CallbackQuery, settings: Settings, db: Database) -> None:
    sub_id = int(cb.data.split(":", 1)[1])
    sub = await db.get_subscription(sub_id)
    if not sub or sub.telegram_id != cb.from_user.id:
        await cb.answer("Подписка не найдена", show_alert=True)
        return
    await cb.message.edit_text(
        "Выбери тариф для продления:",
        reply_markup=kb.renew_menu(settings, sub_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("dorenew:"))
async def cb_dorenew(cb: CallbackQuery, settings: Settings, db: Database, vpn: VPNService) -> None:
    _, raw_id, plan_id = cb.data.split(":", 2)
    sub = await db.get_subscription(int(raw_id))
    plan = settings.plan_by_id(plan_id)
    if not sub or sub.telegram_id != cb.from_user.id or not plan:
        await cb.answer("Ошибка продления", show_alert=True)
        return
    await db.get_or_create_user(cb.from_user.id, cb.from_user.username)

    new_balance = await db.try_debit(cb.from_user.id, plan.price)
    if new_balance is None:
        user = await db.get_user(cb.from_user.id)
        await cb.message.edit_text(
            texts.out_of_funds(plan.price, user.balance if user else 0, settings.currency),
            reply_markup=kb.balance_menu(settings),
        )
        await cb.answer()
        return

    await cb.answer("Продлеваю…")
    try:
        sub = await vpn.renew(sub, plan)
    except Exception as exc:  # noqa: BLE001 - refund on any failure
        await db.adjust_balance(cb.from_user.id, plan.price)
        log.warning("renew failed, refunded %s: %s", plan.price, exc)
        await cb.message.edit_text(
            "Не удалось продлить на сервере. Средства возвращены на баланс, попробуй позже.",
            reply_markup=kb.back_home(),
        )
        return

    await db.record_order(cb.from_user.id, plan.id, plan.price, "renew")
    sub_url = vpn.subscription_url(sub.uuid, sub.vpn_name)
    links = await vpn.links_for(sub)
    await cb.message.edit_text(
        "✅ Подписка продлена!\n\n" + texts.subscription_card(sub, links, sub_url),
        reply_markup=kb.back_home(),
        disable_web_page_preview=True,
    )
