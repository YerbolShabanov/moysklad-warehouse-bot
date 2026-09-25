"""Выгрузка заказов в .xlsx — чтобы кладовщик скачал и распечатал.

Два вида документа:

* карточка одного заказа (`render_order_xlsx`);
* сводный сборочный лист на несколько заказов (`render_batch_xlsx`) —
  лист «Маршрут сборки» для одного прохода по складу и лист «По заказам»
  для раскладки и разбора спорных ситуаций.

Оба настроены на печать: шапка повторяется на каждой странице, таблица
подгоняется по ширине A4, строки идут в порядке обхода склада.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import List, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .formatting import _amount, cell_label, walk_order
from .orders import OrderCard, OrderPosition

HEAD_FILL = PatternFill("solid", fgColor="ECEFF3")
ZEBRA_FILL = PatternFill("solid", fgColor="F8F9FB")
THIN = Side(style="thin", color="C6CAD0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# ширина колонок в символах Excel
COLUMN_WIDTHS = {
    "№": 5,
    "Заказ": 15,
    "Статус": 12,
    "Наименование товара": 62,
    "Штрихкод": 18,
    "Кол-во": 8,
    "Ед.": 6,
    "Ячейка": 14,
}
MAX_ROW_HEIGHT = 45


def order_filename(card: OrderCard) -> str:
    """Безопасное имя файла: «Заказ-1068634272.xlsx»."""
    number = re.sub(r"[^\w\-.]+", "_", card.number, flags=re.UNICODE) or "order"
    return f"Заказ-{number}.xlsx"


def render_order_xlsx(card: OrderCard, compact_slots: bool = True) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сборка"

    ordered = sorted(card.positions, key=walk_order)
    show_uom = any(position.uom and position.uom != "шт" for position in ordered)

    head = ["№", "Наименование товара", "Штрихкод", "Кол-во"]
    if show_uom:
        head.append("Ед.")
    head.append("Ячейка")

    row = _write_heading(sheet, card, len(head))
    header_row = row
    _write_head(sheet, row, head)
    row += 1

    for index, position in enumerate(ordered, 1):
        _write_position(sheet, row, index, position, show_uom, compact_slots, index % 2 == 0)
        row += 1

    _apply_layout(sheet, head, header_row, last_row=row - 1)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_heading(sheet: Worksheet, card: OrderCard, columns: int) -> int:
    """Шапка заказа над таблицей. Возвращает номер строки под неё."""
    lines = [
        (f"Заказ покупателя {card.number}", True),
        (f"Статус: {card.state}", False),
        (f"Дата: {card.moment.strftime('%d.%m.%Y %H:%M') if card.moment else '—'}", False),
        (f"Канал продаж: {card.sales_channel}", False),
    ]
    if card.agent:
        lines.append((f"Контрагент: {card.agent}", False))
    if card.store:
        lines.append((f"Склад: {card.store}", False))

    row = 1
    for text, is_title in lines:
        sheet.merge_cells(
            start_row=row, start_column=1, end_row=row, end_column=columns
        )
        cell = sheet.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=14) if is_title else Font(size=10, color="6E6E6E")
        cell.alignment = Alignment(horizontal="left", vertical="center")
        sheet.row_dimensions[row].height = 22 if is_title else 15
        row += 1

    sheet.row_dimensions[row].height = 6  # пустая строка-разделитель
    return row + 1


def _write_head(sheet: Worksheet, row: int, head: Sequence[str]) -> None:
    for column, title in enumerate(head, 1):
        cell = sheet.cell(row=row, column=column, value=title)
        cell.font = Font(bold=True, size=11)
        cell.fill = HEAD_FILL
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[row].height = 24


def _write_position(
    sheet: Worksheet,
    row: int,
    number: int,
    position: OrderPosition,
    show_uom: bool,
    compact_slots: bool,
    zebra: bool,
) -> None:
    values = [number, position.name, position.barcode or position.code or "—",
              _amount(position.quantity)]
    if show_uom:
        values.append(position.uom or "")
    values.append(cell_label(position, compact_slots))

    for column, value in enumerate(values, 1):
        cell = sheet.cell(row=row, column=column, value=value)
        cell.border = BORDER
        if zebra:
            cell.fill = ZEBRA_FILL
        if column == 1:  # порядковый номер в маршруте сборки
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font = Font(size=11, color="6E6E6E")
        elif column == 2:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.font = Font(size=11)
        elif column == len(values):  # ячейка хранения — главное для сборщика
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font = Font(bold=True, size=11, color="C42828")
        else:
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font = Font(size=11)
            # штрихкод — текстом, иначе Excel съест ведущие нули
            if column == 3:
                cell.number_format = "@"


def _apply_layout(
    sheet: Worksheet,
    head: Sequence[str],
    header_row: int,
    last_row: int,
) -> None:
    for column, title in enumerate(head, 1):
        letter = get_column_letter(column)
        sheet.column_dimensions[letter].width = COLUMN_WIDTHS.get(title, 16)

    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    sheet.auto_filter.ref = (
        f"A{header_row}:{get_column_letter(len(head))}{max(last_row, header_row)}"
    )

    setup = sheet.page_setup
    setup.orientation = "portrait"
    setup.paperSize = sheet.PAPERSIZE_A4
    setup.fitToWidth = 1
    setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.print_title_rows = f"{header_row}:{header_row}"
    sheet.page_margins.left = sheet.page_margins.right = 0.4
    sheet.page_margins.top = sheet.page_margins.bottom = 0.5


# ------------------------------------------------------- сводный сборочный лист


def batch_filename(cards: Sequence[OrderCard]) -> str:
    """«Сборочный-лист-3-заказа-1409-1830.xlsx»."""
    stamp = datetime.now().strftime("%d%m-%H%M")
    return f"Сборочный-лист-{len(cards)}-зак-{stamp}.xlsx"


def render_batch_xlsx(cards: Sequence[OrderCard], compact_slots: bool = True) -> bytes:
    """Сводный лист на несколько заказов."""
    workbook = Workbook()

    route = workbook.active
    route.title = "Маршрут сборки"
    _fill_route_sheet(route, cards, compact_slots)

    by_order = workbook.create_sheet("По заказам")
    _fill_order_sheet(by_order, cards, compact_slots)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _batch_rows(cards: Sequence[OrderCard], compact_slots: bool):
    """Плоский список строк (позиция + номер заказа) по всем заказам."""
    rows = []
    for card in cards:
        for position in card.positions:
            rows.append({
                "order": card.number,
                "state": card.state,
                "name": position.name,
                "barcode": position.barcode or position.code or "—",
                "quantity": _amount(position.quantity),
                "cell": cell_label(position, compact_slots),
                "enough": position.enough,
                "walk": walk_order(position),
            })
    return rows


def _fill_route_sheet(sheet, cards, compact_slots) -> None:
    rows = sorted(_batch_rows(cards, compact_slots), key=lambda r: r["walk"])
    head = ["№", "Наименование товара", "Штрихкод", "Кол-во", "Ячейка", "Заказ"]

    positions = sum(len(card.positions) for card in cards)
    heading = [
        (f"Сводный сборочный лист · заказов: {len(cards)}", True),
        (f"Позиций: {positions} · сформирован {datetime.now():%d.%m.%Y %H:%M}", False),
        ("Строки идут в порядке обхода склада — проходите стеллажи сверху вниз", False),
        ("Номер заказа в последней колонке; раскладка по заказам — на втором листе", False),
    ]
    header_row = _write_lines(sheet, heading, len(head))
    _write_head(sheet, header_row, head)

    for index, row in enumerate(rows, 1):
        line = header_row + index
        _write_cells(
            sheet, line,
            [index, row["name"], row["barcode"], row["quantity"], row["cell"], row["order"]],
            zebra=index % 2 == 0, cell_column=5, barcode_column=3, name_column=2,
        )
    _apply_layout(sheet, head, header_row, header_row + len(rows))


def _fill_order_sheet(sheet, cards, compact_slots) -> None:
    rows = sorted(_batch_rows(cards, compact_slots), key=lambda r: (r["order"], r["walk"]))
    head = ["Заказ", "Статус", "Наименование товара", "Штрихкод", "Кол-во", "Ячейка"]

    heading = [
        ("Раскладка по заказам", True),
        ("Что в какой заказ положить после сборки", False),
    ]
    header_row = _write_lines(sheet, heading, len(head))
    _write_head(sheet, header_row, head)

    previous = None
    for index, row in enumerate(rows, 1):
        line = header_row + index
        new_order = row["order"] != previous
        previous = row["order"]
        _write_cells(
            sheet, line,
            [row["order"] if new_order else "", row["state"] if new_order else "",
             row["name"], row["barcode"], row["quantity"], row["cell"]],
            zebra=index % 2 == 0, cell_column=6, barcode_column=4, name_column=3,
        )
    _apply_layout(sheet, head, header_row, header_row + len(rows))


def _write_lines(sheet: Worksheet, lines, columns: int) -> int:
    row = 1
    for text, is_title in lines:
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
        cell = sheet.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, size=14) if is_title else Font(size=10, color="6E6E6E")
        cell.alignment = Alignment(horizontal="left", vertical="center")
        sheet.row_dimensions[row].height = 22 if is_title else 15
        row += 1
    sheet.row_dimensions[row].height = 6
    return row + 1


def _write_cells(sheet, row, values, zebra, cell_column, barcode_column, name_column) -> None:
    for column, value in enumerate(values, 1):
        cell = sheet.cell(row=row, column=column, value=value)
        cell.border = BORDER
        if zebra:
            cell.fill = ZEBRA_FILL
        if column == name_column:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.font = Font(size=11)
        elif column == cell_column:
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font = Font(bold=True, size=11, color="C42828")
        else:
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font = Font(size=11)
            if column == barcode_column:
                cell.number_format = "@"
