"""Рендер карточки заказа в сообщения Telegram (parse_mode=HTML).

Вёрстка: моноширинная таблица `№ · наименование · штрихкод · кол-во · ячейка`.
Строки идут в порядке обхода склада — по рядам, местам и этажам, чтобы
кладовщик проходил стеллажи подряд, а не возвращался назад.
"""
from __future__ import annotations

from html import escape
from typing import List, Optional, Sequence, Tuple

from . import slots as slot_rules
from .orders import OrderCard, OrderPosition

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3600  # запас на теги <pre> и служебные строки

NO_CELL = "—"
# Telegram переносит длинные строки в <pre>, а не прокручивает их, поэтому
# таблица должна укладываться в ширину экрана. Ширину колонки с названием
# считаем как остаток от этого лимита.
DEFAULT_MAX_WIDTH = 58
MIN_NAME_WIDTH = 14
COLUMN_GAP = "  "

def compact_slot(name: str) -> str:
    """Сокращает стандартное имя ячейки; нестандартные оставляет как есть."""
    return slot_rules.compact(name)


def cell_label(position: OrderPosition, compact_slots: bool = True) -> str:
    """Подпись ячейки для таблицы.

    Если в ячейке лежит меньше, чем нужно по позиции (а достаточной ячейки
    на складе нет), дописываем фактический остаток — иначе сборщик узнает
    о нехватке только у стеллажа.
    """
    slot = position.slot
    if slot is None:
        return NO_CELL
    name = slot_rules.compact(slot.slot_name) if compact_slots else slot.slot_name
    if position.enough:
        return name
    return f"{name} (есть {_amount(slot.stock)} из {_amount(position.quantity)})"


def walk_order(position: OrderPosition) -> Tuple:
    """Ключ сортировки: порядок обхода стеллажей.

    Сначала обычные ячейки по ряду/месту/этажу, затем нестандартные
    («Приёмка 1», «Комплектация стол 2») по алфавиту, в конце — позиции,
    которых нет ни в одной ячейке.
    """
    if not position.slots:
        return (2, 0, 0, 0, "")
    return slot_rules.walk_key(position.slots[0].slot_name)


def render_order(
    card: OrderCard,
    compact_slots: bool = True,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> List[str]:
    """Список сообщений: шапка и таблица позиций в порядке обхода склада."""
    header = _render_header(card)

    if not card.positions:
        return [header + "\n\n<i>В заказе нет позиций.</i>"]

    rows = _table_rows(card.positions, compact_slots, max_width)
    return _pack(header, len(card.positions), rows)


def render_caption(card: OrderCard) -> str:
    """Короткая подпись к файлу: то же, что в шапке, плюс число позиций."""
    cells = sum(1 for position in card.positions if position.slots)
    lines = [_render_header(card), "", f"Позиций: {len(card.positions)}"]
    if cells != len(card.positions):
        lines[-1] += f" · без ячейки: {len(card.positions) - cells}"
    return "\n".join(lines)


# ------------------------------------------------------------------ шапка


def _render_header(card: OrderCard) -> str:
    moment = card.moment.strftime("%d.%m.%Y %H:%M") if card.moment else "—"
    lines = [
        f"📦 <b>Заказ покупателя {escape(card.number)}</b>",
        f"Статус: <b>{escape(card.state)}</b>",
        f"Дата: {moment}",
        f"Канал продаж: {escape(card.sales_channel)}",
    ]
    if card.agent:
        lines.append(f"Контрагент: {escape(card.agent)}")
    if card.store:
        lines.append(f"Склад в заказе: {escape(card.store)}")
    if card.warning:
        lines.append(f"\n⚠️ {escape(card.warning)}")
    return "\n".join(lines)


# --------------------------------------------------------------- таблица


def _table_rows(
    positions: Sequence[OrderPosition],
    compact_slots: bool,
    max_width: int,
) -> List[str]:
    """Строки таблицы (первая — заголовок), выровненные по колонкам."""
    ordered = sorted(positions, key=walk_order)
    show_uom = any(position.uom and position.uom != "шт" for position in ordered)

    head = ["Наименование", "Штрихкод", "Кол"]
    if show_uom:
        head.append("Ед")
    head.append("Ячейка")

    rest = []
    for position in ordered:
        cell = cell_label(position, compact_slots)
        columns = [position.barcode or position.code or "—", _amount(position.quantity)]
        if show_uom:
            columns.append(position.uom or "")
        columns.append(cell)
        rest.append(columns)

    # Ширина колонки с названием — то, что осталось от лимита строки.
    tail_widths = [
        max(len(row[i]) for row in [head[1:]] + rest)
        for i in range(len(head) - 1)
    ]
    gaps = len(COLUMN_GAP) * (len(head) - 1)
    name_width = max_width - sum(tail_widths) - gaps if max_width > 0 else 0
    if 0 < name_width < MIN_NAME_WIDTH:
        name_width = MIN_NAME_WIDTH

    names = [_fit(position.name, name_width) for position in ordered]
    name_column = max([len(head[0])] + [len(n) for n in names])

    widths = [name_column] + tail_widths
    right = {2}  # «Кол» — по правому краю: колонки [название, штрихкод, кол, (ед), ячейка]

    lines = []
    for row in [head] + [[name] + rest[i] for i, name in enumerate(names)]:
        cells = [
            value.rjust(widths[i]) if i in right else value.ljust(widths[i])
            for i, value in enumerate(row)
        ]
        lines.append(COLUMN_GAP.join(cells).rstrip())
    return lines


def _fit(name: str, width: int) -> str:
    """Укорачивает название, вырезая середину.

    Отличительная часть — цвет и размер — стоит в конце («…(бежевый, 28)»),
    а артикул в начале, поэтому обрезать можно только середину.
    """
    if width <= 0 or len(name) <= width:
        return name
    head = (width - 1) // 2
    tail = width - 1 - head
    return name[:head] + "…" + name[-tail:]


# ----------------------------------------------------------- сборка чанков


def _pack(header: str, total: int, table_rows: List[str]) -> List[str]:
    """Раскладывает шапку и таблицу по сообщениям в лимит Telegram."""
    messages: List[str] = []
    head_row, *body_rows = table_rows

    prefix = header + f"\n\n<b>Позиции ({total}):</b>"
    buffer: List[str] = []
    first = True

    def flush() -> None:
        nonlocal buffer, first
        if not buffer:
            return
        table = "<pre>" + escape("\n".join([head_row] + buffer)) + "</pre>"
        messages.append((prefix + "\n" + table) if first else table)
        buffer = []
        first = False

    for row in body_rows:
        pending = sum(len(item) + 1 for item in buffer + [row])
        overhead = len(prefix) + len(head_row) + 40 if first else len(head_row) + 40
        if buffer and pending + overhead > CHUNK_LIMIT:
            flush()
        buffer.append(row)
    flush()

    return messages


def _amount(value: float) -> str:
    """3.0 -> «3», 2.5 -> «2.5»."""
    if value == int(value):
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")
