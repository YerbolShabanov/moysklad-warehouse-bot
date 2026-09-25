"""Разбор имён ячеек адресного хранения и правила выбора ячейки.

Имя ячейки на складе — «Ряд 10 место 9 этаж 2». Отсюда берутся две вещи:

* **порядок обхода** — по ряду, месту и этажу, чтобы сборщик шёл вдоль
  стеллажей и не возвращался;
* **приоритет съёма** — с какого этажа брать товар, если он лежит сразу
  в нескольких ячейках.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

SLOT_RE = re.compile(
    r"^\s*ряд\s*(\d+)\s*место\s*(\d+)\s*этаж\s*(\d+)\s*$",
    re.IGNORECASE,
)

# Третий этаж — упакованные паллеты, их снимают погрузчиком, поэтому он
# худший вариант. Ячейки без этажа («Приёмка 1», «Комплектация стол 2») —
# напольные, брать с них проще, чем гонять погрузчик: ставим перед третьим.
FLOORLESS_RANK = 2.5


def parse(name: str) -> Optional[Tuple[int, int, int]]:
    """«Ряд 10 место 9 этаж 2» -> (10, 9, 2). Нестандартное имя -> None."""
    match = SLOT_RE.match(name or "")
    if not match:
        return None
    row, place, level = match.groups()
    return int(row), int(place), int(level)


def compact(name: str) -> str:
    """«Ряд 10 место 9 этаж 2» -> «Р10-М9-Э2». Нестандартное имя не трогаем."""
    parsed = parse(name)
    if not parsed:
        return name
    row, place, level = parsed
    return f"Р{row}-М{place}-Э{level}"


def floor(name: str) -> Optional[int]:
    parsed = parse(name)
    return parsed[2] if parsed else None


def pick_rank(name: str) -> float:
    """Насколько ячейка удобна для съёма: меньше — лучше.

    Этаж 1 -> 1, этаж 2 -> 2, ячейка без этажа -> 2.5, этаж 3 -> 3.
    """
    level = floor(name)
    return FLOORLESS_RANK if level is None else float(level)


def walk_key(name: str) -> Tuple:
    """Ключ сортировки по маршруту обхода склада."""
    parsed = parse(name)
    if parsed:
        return (0,) + parsed + ("",)
    return (1, 0, 0, 0, name)
