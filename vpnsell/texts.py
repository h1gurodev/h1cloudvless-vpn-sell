"""User-facing Russian text and formatting helpers."""
from __future__ import annotations

import time

from .config import Plan, Settings
from .db import Subscription


def fmt_money(amount: int, currency: str) -> str:
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency.upper(), currency)
    return f"{amount} {symbol}"


def fmt_expiry(expires_at: int) -> str:
    if expires_at <= 0:
        return "бессрочно"
    remaining = expires_at - int(time.time())
    if remaining <= 0:
        return "истекла"
    days = remaining // 86400
    if days >= 1:
        return f"ещё {days} дн."
    hours = max(1, remaining // 3600)
    return f"ещё {hours} ч."


def fmt_quota(traffic_gb: int, devices: int) -> str:
    traffic = "безлимит" if traffic_gb <= 0 else f"{traffic_gb} ГБ"
    dev = "без лимита" if devices <= 0 else f"{devices} устр."
    return f"{traffic}, {dev}"


def plan_line(plan: Plan, currency: str) -> str:
    return f"{plan.title} — {fmt_money(plan.price, currency)} · {fmt_quota(plan.traffic_gb, plan.devices)}"


def welcome(name: str) -> str:
    return (
        f"Привет, {name}! 👋\n\n"
        "Это бот для покупки быстрого и надёжного VPN (VLESS).\n\n"
        "• Подключение в пару кликов\n"
        "• Работает на iOS, Android, Windows, macOS\n"
        "• Одна ссылка-подписка на все устройства\n\n"
        "Выбери действие в меню ниже."
    )


def balance_text(balance: int, currency: str, pay_enabled: bool = False) -> str:
    text = f"💰 Ваш баланс: <b>{fmt_money(balance, currency)}</b>\n\n"
    if pay_enabled:
        text += (
            "Пополни баланс криптовалютой и оплачивай подписки внутри бота. "
            "Нажми «Пополнить»."
        )
    else:
        text += (
            "Пополнение скоро будет доступно. Если нужно пополнить сейчас — "
            "напишите администратору."
        )
    return text


def subscription_card(sub: Subscription, links: list[str], sub_url: str) -> str:
    title = "Пробный период" if sub.is_trial else (sub.plan_id or "Подписка")
    lines = [
        f"🔑 <b>{title}</b>",
        f"Статус: {fmt_expiry(sub.expires_at)}",
        f"Лимиты: {fmt_quota(sub.traffic_gb, sub.devices)}",
    ]
    if sub_url:
        lines.append("")
        lines.append("Ссылка-подписка (добавь в приложение):")
        lines.append(f"<code>{sub_url}</code>")
    if links:
        lines.append("")
        lines.append("Прямые VLESS-ключи:")
        for link in links[:6]:
            lines.append(f"<code>{link}</code>")
    return "\n".join(lines)


HELP_TEXT = (
    "❓ <b>Как подключиться</b>\n\n"
    "1. Купи подписку или активируй пробный период.\n"
    "2. Открой раздел «Мои подписки» и скопируй ссылку-подписку.\n"
    "3. Установи приложение:\n"
    "   • iOS / macOS — <b>Streisand</b> или <b>v2RayTun</b>\n"
    "   • Android — <b>v2RayTun</b> или <b>Hiddify</b>\n"
    "   • Windows — <b>Hiddify</b> или <b>v2RayN</b>\n"
    "4. Вставь ссылку-подписку в приложение и подключись.\n\n"
    "Если что-то не работает — напиши администратору."
)


def out_of_funds(price: int, balance: int, currency: str) -> str:
    return (
        "Недостаточно средств 😔\n\n"
        f"Стоимость: {fmt_money(price, currency)}\n"
        f"Ваш баланс: {fmt_money(balance, currency)}\n\n"
        "Пополни баланс через «Пополнить» и попробуй снова."
    )
