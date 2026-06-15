"""Inline keyboards."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .db import Subscription
from .texts import fmt_expiry, fmt_money


def main_menu(settings: Settings, is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить VPN", callback_data="buy")
    kb.button(text="🔑 Мои подписки", callback_data="subs")
    if settings.trial_days > 0:
        kb.button(text="🎁 Пробный период", callback_data="trial")
    kb.button(text="💰 Баланс", callback_data="balance")
    kb.button(text="❓ Помощь", callback_data="help")
    if is_admin:
        kb.button(text="⚙️ Админка", callback_data="admin")
    kb.adjust(1, 2, 2, 1)
    return kb.as_markup()


def balance_menu(settings: Settings) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if settings.pay_enabled:
        kb.button(text="➕ Пополнить", callback_data="topup")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def topup_menu(settings: Settings) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in settings.topup_amounts:
        kb.button(
            text=f"{fmt_money(amount, settings.pay_currency)}",
            callback_data=f"topup:{amount}",
        )
    kb.button(text="⬅️ Назад", callback_data="balance")
    kb.adjust(2)
    return kb.as_markup()


def pay_link_menu(pay_url: str, uuid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=pay_url)
    kb.button(text="🔄 Проверить оплату", callback_data=f"checkpay:{uuid}")
    kb.button(text="⬅️ В меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def plans_menu(settings: Settings) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in settings.plans:
        kb.button(
            text=f"{plan.title} — {fmt_money(plan.price, settings.currency)}",
            callback_data=f"plan:{plan.id}",
        )
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def confirm_buy(plan_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Оплатить с баланса", callback_data=f"pay:{plan_id}")
    kb.button(text="⬅️ Назад", callback_data="buy")
    kb.adjust(1)
    return kb.as_markup()


def subs_menu(subscriptions: list[Subscription]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sub in subscriptions:
        title = "Пробный" if sub.is_trial else (sub.plan_id or "Подписка")
        kb.button(
            text=f"{title} · {fmt_expiry(sub.expires_at)}",
            callback_data=f"sub:{sub.id}",
        )
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def sub_detail_menu(sub: Subscription) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Продлить", callback_data=f"renew:{sub.id}")
    kb.button(text="📱 QR-код", callback_data=f"qr:{sub.id}")
    kb.button(text="⬅️ К списку", callback_data="subs")
    kb.adjust(2, 1)
    return kb.as_markup()


def renew_menu(settings: Settings, sub_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in settings.plans:
        kb.button(
            text=f"{plan.title} — {fmt_money(plan.price, settings.currency)}",
            callback_data=f"dorenew:{sub_id}:{plan.id}",
        )
    kb.button(text="⬅️ Назад", callback_data=f"sub:{sub_id}")
    kb.adjust(1)
    return kb.as_markup()


def back_home() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 В меню", callback_data="home")
    return kb.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="💳 Начислить баланс", callback_data="admin:credit")
    kb.button(text="🩺 Проверить узел", callback_data="admin:health")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()
