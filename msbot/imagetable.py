"""Отрисовка карточки заказа таблицей-картинкой (PNG).

Telegram переносит длинные строки в текстовых блоках, из-за чего широкая
таблица разваливается. Картинка от этого свободна: названия влезают
целиком, а Telegram показывает её сразу в ленте.
"""
from __future__ import annotations

import io
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from .formatting import cell_label, walk_order, _amount
from .orders import OrderCard, OrderPosition

SCALE = 2  # рисуем крупнее, чтобы текст не мылился после сжатия Telegram

REGULAR_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
BOLD_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

BG = (255, 255, 255)
INK = (23, 23, 23)
MUTED = (110, 110, 110)
GRID = (198, 202, 208)
HEAD_BG = (236, 239, 243)
ZEBRA = (248, 249, 251)
ACCENT = (196, 40, 40)

PAD = 10 * SCALE          # внутренние отступы ячейки
MARGIN = 16 * SCALE       # поля картинки
NAME_MAX = 300 * SCALE    # предел ширины колонки с названием


class FontsMissing(RuntimeError):
    """В системе нет шрифта с кириллицей."""


def _load_font(paths: Sequence[str], size: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise FontsMissing(
        "Не найден TTF-шрифт с кириллицей. Установите DejaVu или Liberation: "
        "apt-get install fonts-dejavu-core"
    )


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _wrap(draw, text: str, font, limit: int) -> List[str]:
    """Переносит текст по словам, не разрывая слова без нужды."""
    if not text:
        return [""]
    lines, current = [], ""
    for word in text.split(" "):
        probe = f"{current} {word}".strip()
        if current and _text_width(draw, probe, font) > limit:
            lines.append(current)
            current = word
        else:
            current = probe
    if current:
        lines.append(current)
    return lines or [""]


def render_order_image(card: OrderCard, compact_slots: bool = True) -> bytes:
    """Возвращает PNG с шапкой заказа и таблицей позиций."""
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    f_title = _load_font(BOLD_FONTS, 17 * SCALE)
    f_meta = _load_font(REGULAR_FONTS, 12 * SCALE)
    f_head = _load_font(BOLD_FONTS, 12 * SCALE)
    f_cell = _load_font(REGULAR_FONTS, 12 * SCALE)
    f_mono = _load_font(BOLD_FONTS, 12 * SCALE)

    ordered = sorted(card.positions, key=walk_order)
    show_uom = any(p.uom and p.uom != "шт" for p in ordered)

    head = ["№", "Наименование товара", "Штрихкод", "Кол-во"]
    if show_uom:
        head.append("Ед.")
    head.append("Ячейка")

    rows: List[List[str]] = []
    for number, position in enumerate(ordered, 1):
        cell = cell_label(position, compact_slots)
        row = [str(number), position.name, position.barcode or position.code or "—",
               _amount(position.quantity)]
        if show_uom:
            row.append(position.uom or "")
        row.append(cell)
        rows.append(row)

    # Ширина колонок: название ограничиваем, остальные — по содержимому
    widths = []
    for index, title in enumerate(head):
        font_for = f_head
        width = _text_width(scratch, title, font_for)
        for row in rows:
            width = max(width, _text_width(scratch, row[index], f_cell))
        widths.append(min(width, NAME_MAX) if index == 1 else width)
    widths = [w + PAD * 2 for w in widths]

    line_height = int((f_cell.size) * 1.35)
    wrapped: List[List[List[str]]] = []
    heights: List[int] = []
    for row in rows:
        cells = [[row[0]], _wrap(scratch, row[1], f_cell, widths[1] - PAD * 2)]
        cells += [[value] for value in row[2:]]
        wrapped.append(cells)
        heights.append(max(len(c) for c in cells) * line_height + PAD)

    header_height = line_height + PAD
    meta_lines = [
        f"Статус: {card.state}",
        f"Дата: {card.moment.strftime('%d.%m.%Y %H:%M') if card.moment else '—'}",
        f"Канал продаж: {card.sales_channel}",
    ]
    if card.agent:
        meta_lines.append(f"Контрагент: {card.agent}")

    title_height = int(f_title.size * 1.5)
    meta_height = len(meta_lines) * int(f_meta.size * 1.45) + PAD

    table_width = sum(widths)
    width = table_width + MARGIN * 2
    height = (MARGIN + title_height + meta_height + header_height
              + sum(heights) + MARGIN)

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    y = MARGIN
    draw.text((MARGIN, y), f"Заказ покупателя {card.number}", font=f_title, fill=INK)
    y += title_height
    for line in meta_lines:
        draw.text((MARGIN, y), line, font=f_meta, fill=MUTED)
        y += int(f_meta.size * 1.45)
    y += PAD

    # Заголовок таблицы
    draw.rectangle([MARGIN, y, MARGIN + table_width, y + header_height], fill=HEAD_BG)
    x = MARGIN
    for index, title in enumerate(head):
        draw.text((x + PAD, y + PAD // 2), title, font=f_head, fill=INK)
        x += widths[index]
    y += header_height

    # Строки
    for row_index, cells in enumerate(wrapped):
        row_height = heights[row_index]
        if row_index % 2:
            draw.rectangle([MARGIN, y, MARGIN + table_width, y + row_height], fill=ZEBRA)
        x = MARGIN
        for column, lines in enumerate(cells):
            is_cell_column = column == len(cells) - 1
            font = f_mono if is_cell_column else f_cell
            colour = ACCENT if is_cell_column else INK
            for line_index, line in enumerate(lines):
                draw.text(
                    (x + PAD, y + PAD // 2 + line_index * line_height),
                    line, font=font, fill=colour,
                )
            x += widths[column]
        y += row_height

    # Сетка
    table_bottom = y
    x = MARGIN
    for column_width in widths[:-1]:
        x += column_width
        draw.line([(x, MARGIN + title_height + meta_height), (x, table_bottom)],
                  fill=GRID, width=1)
    y = MARGIN + title_height + meta_height
    draw.line([(MARGIN, y), (MARGIN + table_width, y)], fill=GRID, width=1)
    y += header_height
    for row_height in heights:
        draw.line([(MARGIN, y), (MARGIN + table_width, y)], fill=GRID, width=1)
        y += row_height
    draw.rectangle([MARGIN, MARGIN + title_height + meta_height,
                    MARGIN + table_width, table_bottom], outline=GRID, width=1)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
