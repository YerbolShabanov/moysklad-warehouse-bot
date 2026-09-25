"""Единая точка отправки карточки заказа в Telegram.

Формат задаётся `ORDER_FORMAT`: xlsx (файл для печати), text (таблица
в сообщении) или image (та же таблица картинкой). Логика одна и та же
и для ручного запроса, и для автоматической рассылки.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import Settings
from .excel import order_filename, render_order_xlsx
from .formatting import render_caption, render_order
from .orders import OrderCard

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


async def send_card(
    bot: Bot,
    chat_id: int,
    card: OrderCard,
    settings: Settings,
    prefix: str = "",
) -> None:
    """Отправляет заказ в чат в формате из настроек."""
    fmt = (settings.order_format or "xlsx").lower()

    if fmt == "text":
        chunks = render_order(card, settings.compact_slots, settings.table_width)
        chunks[0] = prefix + chunks[0]
        for chunk in chunks:
            await bot.send_message(chat_id, chunk)
        return

    caption = (prefix + render_caption(card))[:CAPTION_LIMIT]

    if fmt == "image":
        from .imagetable import render_order_image  # Pillow нужен только здесь

        payload = render_order_image(card, settings.compact_slots)
        await bot.send_photo(
            chat_id,
            BufferedInputFile(payload, filename=order_filename(card).replace(".xlsx", ".png")),
            caption=caption,
        )
        return

    payload = render_order_xlsx(card, settings.compact_slots)
    await bot.send_document(
        chat_id,
        BufferedInputFile(payload, filename=order_filename(card)),
        caption=caption,
    )
