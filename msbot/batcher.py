"""Сводный сборочный лист: копит заказы Kaspi и шлёт их одним файлом.

Заказы в очередь кладёт `NewOrderNotifier` — в момент, когда он видит
заказ, впервые попавший в нужный статус (Новый/Упаковка) с маршрутом
`batch` (см. `msbot.routing`). Сам батчер МойСклад не опрашивает.

Почему не живой опрос «кто сейчас в Упаковка/Новый». Так было раньше, и
это раняне: заказ может пройти Упаковка → Передача за минуты — быстрее,
чем интервал опроса (`BATCH_INTERVAL`). Живой запрос «кто СЕЙЧАС в этом
статусе» в момент опроса такой заказ уже не находит — он никогда не
попадает в лист, без единой ошибки в логе. `notifier` же идёт по ленте
`updated` и гарантированно видит каждое изменение статуса хотя бы раз
(проверено и покрыто тестом), поэтому детектирование отдано ему, а
батчер отвечает только за накопление и отправку.

Лист уходит, когда набралось `BATCH_SIZE` заказов либо когда самый старый
из накопленных ждёт дольше `BATCH_MAX_WAIT_MINUTES` — иначе в спокойные
часы заказ мог бы пролежать до следующего дня.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import Settings
from .excel import batch_filename, render_batch_xlsx
from .orders import OrderCard, OrderService

log = logging.getLogger(__name__)

SENT_LIMIT = 2000
QUEUE_LIMIT = 500  # аварийный потолок — не должен достигаться в норме
CAPTION_LIMIT = 1024


class PickingBatcher:
    def __init__(
        self,
        bot: Bot,
        service: OrderService,
        settings: Settings,
        state_path: Optional[str] = None,
    ) -> None:
        self._bot = bot
        self._service = service
        self._settings = settings
        self._clock = settings.clock()
        self._interval = max(settings.batch_interval, 5)
        self._size = max(settings.batch_size, 1)
        self._wait = timedelta(minutes=max(settings.batch_max_wait, 0))
        self._state_path = Path(state_path or settings.batch_state_file)

        self._queue: List[Dict[str, Any]] = []
        self._queued_ids: Set[str] = set()
        self._sent: List[str] = []
        self._sent_set: Set[str] = set()

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
        self._queue = list(data.get("queue") or [])
        self._queued_ids = {row["id"] for row in self._queue}

    def _save_state(self) -> None:
        payload = {
            "sent": self._sent[-SENT_LIMIT:],
            "queue": self._queue,
        }
        try:
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Не удалось сохранить %s: %s", self._state_path, exc)

    def _remember_sent(self, order_id: str) -> None:
        if order_id in self._sent_set:
            return
        self._sent.append(order_id)
        self._sent_set.add(order_id)
        if len(self._sent) > SENT_LIMIT * 2:
            dropped = self._sent[:-SENT_LIMIT]
            self._sent = self._sent[-SENT_LIMIT:]
            self._sent_set.difference_update(dropped)

    # --------------------------------------------------------------- очередь

    def enqueue(self, order: Dict[str, Any]) -> None:
        """Кладёт заказ в очередь на сводный лист. Дублей не создаёт.

        Вызывается из `NewOrderNotifier` синхронно (без сети) в момент
        обнаружения маршрута `batch` — до того, как заказ успеет уйти в
        другой статус.
        """
        order_id = order["id"]
        if order_id in self._queued_ids or order_id in self._sent_set:
            return
        if len(self._queue) >= QUEUE_LIMIT:
            log.warning(
                "Сводный лист: очередь переполнена (%s) — заказ %s не добавлен, "
                "проверьте, не завис ли батчер",
                QUEUE_LIMIT, order.get("name"),
            )
            return
        self._queue.append({
            "id": order_id,
            "name": order.get("name"),
            "created": order.get("created"),
            # Момент постановки в очередь — от него отсчитывается
            # BATCH_MAX_WAIT_MINUTES, а не от создания заказа в МойСклад:
            # заказы разбираются notifier-ом в порядке `updated`, а не
            # `created`, так что более старый по МойСклад заказ может
            # встать в очередь позже более нового.
            "queued_at": self._clock.to_moysklad(self._clock.now_display()),
        })
        self._queued_ids.add(order_id)
        self._save_state()
        log.info(
            "Сводный лист: заказ %s поставлен в очередь (всего в очереди: %s)",
            order.get("name"), len(self._queue),
        )

    # ----------------------------------------------------------------- запуск

    async def start(self) -> None:
        self._load_state()
        log.info(
            "Сводный лист: до %s заказов, отправка при заполнении или через %s мин"
            " (в очереди на старте: %s)",
            self._size, int(self._wait.total_seconds() // 60), len(self._queue),
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("Сводный лист: непредвиденная ошибка")
            await asyncio.sleep(self._interval)

    async def tick(self) -> Optional[int]:
        """Один круг: смотрим очередь и решаем, пора ли отправлять."""
        if not self._queue:
            return None
        if len(self._queue) < self._size and not self._waited_enough(self._queue[0]):
            return None

        batch = self._queue[: self._size]
        sent = await self._publish(batch)
        if not sent:
            return None
        self._queue = self._queue[len(batch):]
        self._save_state()
        return len(batch)

    def _waited_enough(self, oldest: Dict[str, Any]) -> bool:
        queued_at = self._clock.parse(oldest.get("queued_at") or oldest.get("created"))
        if not queued_at:
            return True
        return self._clock.now_display() - queued_at >= self._wait

    async def _publish(self, orders: List[Dict[str, Any]]) -> bool:
        cards: List[OrderCard] = []
        for order in orders:
            card = await self._service.card_by_number(order["name"])
            if card is None:
                # Заказ мог быть удалён/архивирован между постановкой в
                # очередь и отправкой — не блокируем остальных, пропускаем.
                log.warning("Сводный лист: заказ %s не найден, пропущен", order.get("name"))
                continue
            if card.slots_failed:
                # Без ячеек лист бесполезен — подождём следующего круга,
                # заказ остаётся в очереди.
                log.warning("Сводный лист отложен: %s", card.warning)
                return False
            cards.append(card)

        if not cards:
            return True  # все пропущены как несуществующие — из очереди убрать

        payload = render_batch_xlsx(cards, self._settings.compact_slots)
        await self._bot.send_document(
            self._settings.notify_chat_id,
            BufferedInputFile(payload, filename=batch_filename(cards)),
            caption=self._caption(cards)[:CAPTION_LIMIT],
        )
        for order in orders:
            self._remember_sent(order["id"])
        log.info(
            "Сводный лист отправлен: %s заказов (%s)",
            len(cards), ", ".join(c.number for c in cards),
        )
        return True

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
