"""Куда направить заказ: в сводный лист, экспрессом или поштучно.

Три маршрута:

* **batch** — Kaspi без галочки «Экспресс доставка». Такие заказы копятся
  и уходят одним сводным файлом: сборщику удобнее пройти склад один раз
  на несколько заказов, чем бегать по одному.
* **express** — Kaspi с галочкой. Ждать нельзя, отправляем сразу и
  отдельно, с пометкой.
* **individual** — всё остальное (ОПТ и прочие каналы) — отдельным
  сообщением на заказ.

Статусы, по которым заказ считается готовым к сборке, у каналов разные.
Оптовый заказ заводят пустым и наполняют позициями вручную десятки минут,
поэтому по «Новый» его отдавать нельзя — уйдёт пустой файл. Признак
готовности для ОПТ — отдельный статус (`OPT_STATES`, по умолчанию
«Моя Доставка»); для маркетплейса остаются `NOTIFY_STATES`.
"""
from __future__ import annotations

from typing import Any, Dict

BATCH = "batch"
EXPRESS = "express"
INDIVIDUAL = "individual"


def channel_name(order: Dict[str, Any]) -> str:
    return ((order.get("salesChannel") or {}).get("name") or "").strip()


def agent_name(order: Dict[str, Any]) -> str:
    return ((order.get("agent") or {}).get("name") or "").strip()


def is_express(order: Dict[str, Any], attribute: str) -> bool:
    """Стоит ли галочка «Экспресс доставка».

    Когда галочка снята, МойСклад не присылает атрибут вовсе — поэтому
    отсутствие считаем отрицанием, а не «неизвестно».
    """
    for attr in order.get("attributes") or []:
        if (attr.get("name") or "").strip().lower() == attribute.strip().lower():
            return bool(attr.get("value"))
    return False


def is_kaspi(order: Dict[str, Any], channel: str) -> bool:
    """Заказ маркетплейса: сверяем и канал продаж, и контрагента.

    В выгрузке они совпадают (99 заказов из 100), но полагаться на одно
    поле рискованно: достаточно совпадения любого.
    """
    target = channel.strip().lower()
    return target in (channel_name(order).lower(), agent_name(order).lower())


def is_opt(order: Dict[str, Any], channel: str) -> bool:
    """Оптовый заказ: сверяем канал продаж (контрагенты у ОПТ все разные)."""
    return channel_name(order).lower() == channel.strip().lower()


def route(order: Dict[str, Any], channel: str, express_attribute: str) -> str:
    if not is_kaspi(order, channel):
        return INDIVIDUAL
    return EXPRESS if is_express(order, express_attribute) else BATCH
