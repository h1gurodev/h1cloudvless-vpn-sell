"""Global error handling: never let an unhandled exception drop silently."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery, ErrorEvent

log = logging.getLogger("vpnsell.errors")
router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Log the traceback and show the user a friendly message instead of crashing."""
    log.exception("Unhandled error: %s", event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            cb: CallbackQuery = update.callback_query
            await cb.answer("Произошла ошибка. Попробуй ещё раз.", show_alert=True)
        elif update.message is not None:
            await update.message.answer("Произошла ошибка. Попробуй ещё раз позже.")
    except Exception:  # noqa: BLE001 - the notification itself may fail; swallow it
        pass
    # Returning True marks the error handled so polling keeps running.
    return True
