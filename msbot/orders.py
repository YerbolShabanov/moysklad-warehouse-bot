"""Сборка карточки заказа покупателя с адресами хранения позиций."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .moysklad import MoySkladClient, MoySkladError
from . import slots as slot_rules
from .timezones import Clock

log = logging.getLogger(__name__)


@dataclass
class SlotStock:
    """Остаток одной позиции в конкретной ячейке."""
    slot_name: str
    stock: float

    @property
    def floor(self) -> Optional[int]:
        return slot_rules.floor(self.slot_name)


@dataclass
class OrderPosition:
    name: str
    code: str
    barcode: str
    quantity: float
    uom: str
    reserve: float = 0.0
    slots: List[SlotStock] = field(default_factory=list)

    @property
    def slot(self) -> Optional[SlotStock]:
        """Ячейка, откуда брать товар."""
        return self.slots[0] if self.slots else None

    @property
    def enough(self) -> bool:
        """Хватает ли в выбранной ячейке на всё количество позиции."""
        return bool(self.slot) and self.slot.stock >= self.quantity


@dataclass
class OrderCard:
    id: str
    number: str
    state: str
    moment: Optional[datetime]
    sales_channel: str
    agent: str
    store: str
    positions: List[OrderPosition] = field(default_factory=list)
    warning: str = ""
    #: остатки по ячейкам получить не удалось — карточка без адресов хранения
    slots_failed: bool = False


class _TTLCache:
    """Простейший кэш значения с TTL."""

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._value = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, loader):
        async with self._lock:
            now = time.monotonic()
            if self._value is None or now >= self._expires_at:
                self._value = await loader()
                self._expires_at = now + self._ttl
            return self._value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0


class OrderService:
    """Достаёт заказ и раскладывает его позиции по ячейкам адресного склада."""

    def __init__(self, client: MoySkladClient, store_name: str, slots_ttl: int = 600,
                 store_ttl: int = 3600, clock: Optional[Clock] = None) -> None:
        self._client = client
        self._store_name = store_name
        self._clock = clock or Clock()
        self._store_cache = _TTLCache(store_ttl)
        self._slots_cache = _TTLCache(slots_ttl)

    # ---------------------------------------------------------- адресный склад

    async def _get_store(self) -> Dict:
        async def loader():
            store = await self._client.find_store(self._store_name)
            if not store:
                raise MoySkladError(
                    f"Склад «{self._store_name}» не найден в МойСклад. "
                    "Проверьте название в переменной MOYSKLAD_STORE_NAME."
                )
            return store

        return await self._store_cache.get(loader)

    async def _get_slot_names(self) -> Dict[str, str]:
        """id ячейки -> её номер (наименование)."""

        async def loader():
            store = await self._get_store()
            slots = await self._client.get_slots(store["id"])
            return {slot["id"]: slot.get("name", "") for slot in slots}

        return await self._slots_cache.get(loader)

    def refresh_caches(self) -> None:
        self._store_cache.invalidate()
        self._slots_cache.invalidate()

    async def check_connection(self) -> str:
        store = await self._get_store()
        slots = await self._get_slot_names()
        return f"{store.get('name')} · ячеек: {len(slots)}"

    # ------------------------------------------------------------------ заказы

    async def card_by_number(self, number: str) -> Optional[OrderCard]:
        order = await self._client.find_order_by_name(number)
        if not order:
            return None
        return await self._build_card(order)

    async def card_by_id(self, order_id: str) -> OrderCard:
        order = await self._client.get_order(order_id)
        return await self._build_card(order)

    async def search(self, query: str, limit: int = 10) -> List[Tuple[str, str, str]]:
        """Список (id, номер, краткое описание) для выбора заказа."""
        rows = await self._client.search_orders(query, limit=limit)
        result = []
        for row in rows:
            moment = self._clock.parse(row.get("moment"))
            parts = [_name_of(row.get("state")) or "без статуса"]
            if moment:
                parts.append(moment.strftime("%d.%m.%Y"))
            agent = _name_of(row.get("agent"))
            if agent:
                parts.append(agent)
            result.append((row["id"], row.get("name", "—"), " · ".join(parts)))
        return result

    async def _build_card(self, order: Dict) -> OrderCard:
        positions_raw = await self._client.get_order_positions(order["id"])

        positions: List[OrderPosition] = []
        assortment_ids: List[str] = []
        by_assortment: Dict[str, List[OrderPosition]] = {}

        for row in positions_raw:
            assortment = row.get("assortment") or {}
            position = OrderPosition(
                name=assortment.get("name") or "Без названия",
                code=assortment.get("code") or "",
                barcode=_first_barcode(assortment),
                quantity=float(row.get("quantity") or 0),
                uom=_name_of((assortment.get("uom") or {})) or "шт",
                reserve=float(row.get("reserve") or 0),
            )
            positions.append(position)

            assortment_id = assortment.get("id")
            if assortment_id:
                if assortment_id not in by_assortment:
                    assortment_ids.append(assortment_id)
                    by_assortment[assortment_id] = []
                by_assortment[assortment_id].append(position)

        warning = ""
        slots_failed = False
        if assortment_ids:
            try:
                await self._fill_slots(assortment_ids, by_assortment)
            except MoySkladError as exc:
                warning = f"Ячейки не получены: {exc}"
                slots_failed = True
                log.warning("Не удалось получить остатки по ячейкам: %s", exc)

        return OrderCard(
            id=order["id"],
            number=order.get("name") or "—",
            state=_name_of(order.get("state")) or "без статуса",
            moment=self._clock.parse(order.get("moment")),
            sales_channel=_name_of(order.get("salesChannel")) or "не указан",
            agent=_name_of(order.get("agent")) or "",
            store=_name_of(order.get("store")) or "",
            positions=positions,
            warning=warning,
            slots_failed=slots_failed,
        )

    async def _fill_slots(
        self,
        assortment_ids: List[str],
        by_assortment: Dict[str, List[OrderPosition]],
    ) -> None:
        store = await self._get_store()
        slot_names = await self._get_slot_names()
        rows = await self._client.stock_by_slot(store["id"], assortment_ids)

        for row in rows:
            assortment_id = row.get("assortmentId")
            slot_id = row.get("slotId")
            if not assortment_id or not slot_id:
                continue
            targets = by_assortment.get(assortment_id)
            if not targets:
                continue
            slot_stock = SlotStock(
                slot_name=slot_names.get(slot_id) or slot_id[:8],
                stock=float(row.get("stock") or 0),
            )
            for position in targets:
                position.slots.append(slot_stock)

        # Порядок съёма. Сначала отсекаем ячейки, где товара не хватает на
        # всю позицию: сборщик не должен идти туда, где придётся добирать.
        # Внутри достаточных действует приоритет этажей — на третьем лежат
        # упакованные паллеты, их снимают погрузчиком. Если достаточной
        # ячейки нет вовсе, порядок тот же, но по остатку — позиция будет
        # помечена как неполная.
        for positions in by_assortment.values():
            for position in positions:
                needed = position.quantity
                position.slots.sort(
                    key=lambda s: (
                        s.stock < needed,
                        slot_rules.pick_rank(s.slot_name),
                        -s.stock,
                        s.slot_name,
                    )
                )


def _first_barcode(assortment: Dict) -> str:
    """Первый штрихкод из карточки товара.

    МойСклад отдаёт `barcodes` как список объектов с одним ключом —
    типом кода: [{"ean13": "469…"}, {"code128": "469…"}].
    """
    for entry in assortment.get("barcodes") or []:
        for value in (entry or {}).values():
            if value:
                return str(value)
    return ""


def _name_of(meta_object: Optional[Dict]) -> str:
    if not meta_object:
        return ""
    return meta_object.get("name") or ""
