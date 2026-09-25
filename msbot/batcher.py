"""Сводный сборочный лист: копит заказы Kaspi и шлёт их одним файлом.

Почему пул, а не лента изменений. Состав листа — это всегда «что прямо
сейчас лежит в статусах Новый/Упаковка». Заказ собрали и перевели дальше —
он сам выпадает из следующего листа, никакого отдельного учёта не нужно.
Поэтому здесь отдельный запрос с фильтрами по статусу, каналу и галочке
«Экспресс доставка», а не общая лента `updated`.

Лист уходит, когда набралось `BATCH_SIZE` заказов либо когда самый старый
из накопленных ждёт дольше `BATCH_MAX_WAIT_MINUTES` — иначе в спокойные
часы заказ мог бы пролежать до следующего дня.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import Settings
from .excel import batch_filename, render_batch_xlsx
from .moysklad import MoySkladClient, MoySkladError
from .orders import OrderCard, OrderService

log = logging.getLogger(__name__)

SENT_LIMIT = 2000
CAPTION_LIMIT = 1024


class PickingBatcher:
    def __init__(
        self,
        bot: Bot,
        client: MoySkladClient,
        service: OrderService,
        settings: Settings,
        state_path: Optional[str] = None,
    ) -> None:
        self._bot = bot
        self._client = client
        self._service = service
        self._settings = settings
        self._clock = settings.clock()
        self._interval = max(settings.batch_interval, 20)
        self._size = max(settings.batch_size, 1)
        self._wait = timedelta(minutes=max(settings.batch_max_wait, 0))
        self._state_path = Path(state_path or settings.batch_state_file)

        self._sent: List[str] = []
        self._sent_set: Set[str] = set()
        self._filter: Optional[str] = None

    # -------------------------------------------------------------- состояние

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s", self._state_path, exc)
            return
        self._sent = list(data.get("sent") or [])
        self._sent_set = set(self._sent)

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps({"sent": self._sent[-SENT_LIMIT:]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Не удалось сохранить %s: %s", self._state_path, exc)

    def _remember(self, order_id: str) -> None:
        if order_id in self._sent_set:
            return
        self._sent.append(order_id)
        self._sent_set.add(order_id)
        if len(self._sent) > SENT_LIMIT * 2:
            dropped = self._sent[:-SENT_LIMIT]
            self._sent = self._sent[-SENT_LIMIT:]
            self._sent_set.difference_update(dropped)

    # ---------------------------------------------------------------- фильтр

    async def _pool_filter(self) -> str:
        """Фильтр «статус Новый/Упаковка + канал Kaspi + экспресс снят».

        Собирается один раз: id статусов, канала и доп. поля в аккаунте не
        меняются, а лишний запрос на каждом круге ни к чему.
        """
        if self._filter:
            return self._filter

        base = self._client.base_url
        meta = await self._client.get("/entity/customerorder/metadata")
        wanted = {s.strip().lower() for s in self._settings.notify_states}
        parts = [
            f"state={base}/entity/customerorder/metadata/states/{st['id']}"
            for st in meta.get("states", [])
            if st.get("name", "").strip().lower() in wanted
        ]
        if not parts:
            raise MoySkladError(
                f"Статусы {', '.join(self._settings.notify_states)} не найдены в МойСклад"
            )

        channels = await self._client.get(
            "/entity/saleschannel", {"filter": f"name={self._settings.kaspi_channel}", "limit": 2}
        )
        rows = channels.get("rows") or []
        if not rows:
            raise MoySkladError(f"Канал продаж «{self._settings.kaspi_channel}» не найден")
        parts.append(f"salesChannel={base}/entity/saleschannel/{rows[0]['id']}")

        attributes = await self._client.get("/entity/customerorder/metadata/attributes")
        target = self._settings.express_attribute.strip().lower()
        attribute = next(
            (a for a in attributes.get("rows", [])
             if (a.get("name") or "").strip().lower() == target), None
        )
        if not attribute:
            raise MoySkladError(
                f"Доп. поле «{self._settings.express_attribute}» не найдено в заказе покупателя"
            )
        parts.append(
            f"{base}/entity/customerorder/metadata/attributes/{attribute['id']}=false"
        )

        self._filter = ";".join(parts)
        log.info("Сводный лист: фильтр пула собран (%s статуса)", len(parts) - 2)
        return self._filter

    # ----------------------------------------------------------------- запуск

    async def start(self) -> None:
        self._load_state()
        log.info(
            "Сводный лист: до %s заказов, отправка при заполнении или через %s мин",
            self._size, int(self._wait.total_seconds() // 60),
        )
        while True:
            try:
                await self.tick()
            except MoySkladError as exc:
                log.warning("Сводный лист: МойСклад недоступен — %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("Сводный лист: непредвиденная ошибка")
            await asyncio.sleep(self._interval)

    async def tick(self) -> Optional[int]:
        """Один круг: смотрим пул и решаем, пора ли отправлять."""
        pending = await self._pending()
        if not pending:
            return None

        if len(pending) < self._size and not self._waited_enough(pending[0]):
            return None

        batch = pending[: self._size]
        await self._publish(batch)
        return len(batch)

    async def _pending(self) -> List[Dict[str, Any]]:
        payload = await self._client.get(
            "/entity/customerorder",
            {
                "filter": await self._pool_filter(),
                "order": "created,asc",
                "limit": 100,
                "expand": "state,salesChannel,agent,store",
            },
        )
        return [r for r in (payload.get("rows") or []) if r["id"] not in self._sent_set]

    def _waited_enough(self, oldest: Dict[str, Any]) -> bool:
        created = self._clock.parse(oldest.get("created"))
        if not created:
            return True
        return self._clock.now_display() - created >= self._wait

    async def _publish(self, orders: List[Dict[str, Any]]) -> None:
        cards: List[OrderCard] = []
        for order in orders:
            card = await self._service.card_by_id(order["id"])
            if card.slots_failed:
                # Без ячеек лист бесполезен — подождём следующего круга.
                log.warning("Сводный лист отложен: %s", card.warning)
                return
            cards.append(card)

        payload = render_batch_xlsx(cards, self._settings.compact_slots)
        await self._bot.send_document(
            self._settings.notify_chat_id,
            BufferedInputFile(payload, filename=batch_filename(cards)),
            caption=self._caption(cards)[:CAPTION_LIMIT],
        )
        for card in cards:
            self._remember(card.id)
        self._save_state()
        log.info(
            "Сводный лист отправлен: %s заказов (%s)",
            len(cards), ", ".join(c.number for c in cards),
        )

    def _caption(self, cards: List[OrderCard]) -> str:
        positions = sum(len(c.positions) for c in cards)
        pieces = sum(p.quantity for c in cards for p in c.positions)
        numbers = ", ".join(c.number for c in cards)
        return (
            f"🧾 <b>Сводный сборочный лист</b>\n\n"
            f"Заказов: <b>{len(cards)}</b> · позиций: {positions} · штук: {pieces:g}\n"
            f"Канал: {self._settings.kaspi_channel} (без экспресса)\n\n"
            f"Заказы: {numbers}"
        )
