"""Bot bootstrap: builds the dispatcher, injects dependencies, runs polling."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from ..config import Settings, load_settings
from ..db import Database
from ..h1cloud import H1CloudClient
from ..payments import PaymentClient
from ..poller import run_poller
from ..vpn_service import VPNService
from . import admin, errors, handlers, topup

log = logging.getLogger("vpnsell")


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота / меню"),
        ]
    )


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    db = Database(settings.db_path)
    await db.connect()

    h1 = H1CloudClient(
        settings.h1_api_url,
        settings.h1_api_token,
        verify_ssl=settings.h1_verify_ssl,
        timeout=settings.h1_request_timeout,
    )
    vpn = VPNService(settings, db, h1)

    pay = PaymentClient(
        settings.pay_base_url,
        settings.pay_project_uuid,
        settings.pay_api_key,
        user_agent=settings.pay_user_agent,
        timeout=settings.h1_request_timeout,
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    # Dependencies injected into every handler via aiogram's kwargs.
    dp["settings"] = settings
    dp["db"] = db
    dp["vpn"] = vpn
    dp["h1"] = h1
    dp["pay"] = pay

    dp.include_router(admin.router)
    dp.include_router(topup.router)
    dp.include_router(handlers.router)
    dp.include_router(errors.router)

    poller_task = None
    if settings.pay_enabled:
        poller_task = asyncio.create_task(run_poller(settings, db, pay, bot))
        log.info("Crypto payments enabled (2328.io, polling mode)")
    else:
        log.info("Crypto payments disabled (set PAY_ENABLED=true + keys to enable)")

    await _set_commands(bot)
    log.info("vpnsell bot starting (admins=%s)", sorted(settings.admin_ids))
    try:
        await dp.start_polling(
            bot, drop_pending_updates=settings.drop_pending_updates
        )
    finally:
        if poller_task is not None:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        await pay.close()
        await h1.close()
        await db.close()
        await bot.session.close()
