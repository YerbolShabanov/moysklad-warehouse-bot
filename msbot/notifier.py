"""Оповещение склада о заказах, пришедших в работу.

МойСклад не шлёт пуши без публичного вебхук-адреса, поэтому бот сам
опрашивает API по расписанию.

Отслеживаем не появление заказа, а его **попадание в нужный статус**:
заказы Kaspi создаются в «Упаковка» и только потом переходят в «Передача»,
а оптовые заводятся как «Новый». Если смотреть лишь на момент создания,
всё, что меняет статус позже, склад никогда не увидит. Поэтому опрос идёт
по полю `updated`, а отправленное запоминается парой «заказ + статус»: один и тот же переход
не повторяется никогда (даже если статус вернулся обратно — иначе мигание
статусов засыпало бы чат), а следующий переход доезжает.

Отсечка (`NOTIFY_SINCE`) не даёт залить чат историей: по умолчанию бот
берёт изменения с начала текущего дня и никогда не отступает назад.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot

from .batcher import PickingBatcher
from .config import Settings
from .delivery import send_card
from .moysklad import MoySkladClient, MoySkladError
from . import routing
from .orders import OrderService
from .timezones import MS_DATETIME as MS_FMT, Clock

log = logging.getLogger(__name__)

SEEN_LIMIT = 1000  # сколько пар «заказ+статус» держим, чтобы не слать дважды
CLAMP_WINDOW = timedelta(hours=24)  # насколько отматываем границу при расхождении часов
PAGE = 100
# Карточка без ячеек сборщику бесполезна. Если отчёт по остаткам не
# ответил, откладываем заказ и пробуем на следующем круге опроса. После
# стольких попыток всё же отправляем — с предупреждением, но не теряем.
MAX_SLOT_ATTEMPTS = 5


class NewOrderNotifier:
    def __init__(
        self,
        bot: Bot,
        client: MoySkladClient,
        service: OrderService,
        settings: Settings,
        batcher: "PickingBatcher",
        state_path: Optional[str] = None,
    ) -> None:
        self._bot = bot
        self._client = client
        self._service = service
        self._settings = settings
        self._batcher = batcher
        self._chat_id = settings.notify_chat_id
        self._interval = max(settings.poll_interval, 15)
        self._states = {s.strip().lower() for s in settings.notify_states if s.strip()}
        self._opt_states = {s.strip().lower() for s in settings.opt_states if s.strip()}
        self._state_path = Path(state_path or settings.state_file)
        self._since = (settings.notify_since or "today").strip()
        self._clock = settings.clock()

        self._mark: Optional[str] = None
        self._seen: List[str] = []
        self._seen_set: Set[str] = set()
        self._known_orders: Set[str] = set()
        self._attempts: Dict[str, int] = {}

    # -------------------------------------------------------------- состояние

    @staticmethod
    def _key(order_id: str, state: str) -> str:
        return f"{order_id}:{state.strip().lower()}"

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s", self._state_path, exc)
            return
        # last_created — от прежней версии, когда опрос шёл по дате создания
        self._mark = data.get("last_updated") or data.get("last_created")
        self._seen = list(data.get("seen") or [])
        self._seen_set = set(self._seen)
        self._known_orders = {key.split(":", 1)[0] for key in self._seen}

    def _save_state(self) -> None:
        payload = {"last_updated": self._mark, "seen": self._seen[-SEEN_LIMIT:]}
        try:
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Не удалось сохранить %s: %s", self._state_path, exc)

    def _remember(self, key: str) -> None:
        if key in self._seen_set:
            return
        self._seen.append(key)
        self._seen_set.add(key)
        self._known_orders.add(key.split(":", 1)[0])
        if len(self._seen) > SEEN_LIMIT * 2:
            dropped = self._seen[:-SEEN_LIMIT]
            self._seen = self._seen[-SEEN_LIMIT:]
            self._seen_set.difference_update(dropped)

    # ----------------------------------------------------------------- запуск

    def baseline(self, now: Optional[datetime] = None) -> str:
        """Граница, раньше которой изменения не рассылаем.

        `today` — начало текущего дня **по часам склада**, `now` — момент
        запуска, либо явная дата (её считаем уже записанной в поясе
        аккаунта). Результат всегда в поясе аккаунта МойСклад: фильтры API
        трактуются именно в нём и смещение в них указать нельзя.
        """
        if self._since == "now":
            return self._clock.to_moysklad(now or self._clock.now_display())
        if self._since == "today":
            return self._clock.to_moysklad(self._clock.today_start(now))
        return self._since

    async def start(self) -> None:
        """Инициализирует точку отсчёта и уходит в бесконечный опрос.

        Если на старте МойСклад недоступен (протух токен, сеть), пробуем
        снова на следующем круге: иначе одна ошибка в момент запуска молча
        выключила бы рассылку до перезапуска процесса.
        """
        while True:
            try:
                await self.start_once()
                break
            except MoySkladError as exc:
                log.warning("Оповещения: не удалось стартовать — %s", exc)
                await asyncio.sleep(self._interval)

        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._tick()
            except MoySkladError as exc:
                log.warning("Оповещения: МойСклад недоступен — %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("Оповещения: непредвиденная ошибка")

    async def start_once(self, now: Optional[datetime] = None) -> None:
        """Готовит точку отсчёта и делает первый проход."""
        self._load_state()

        baseline = await self._safe_baseline(now)
        if not self._mark or self._mark < baseline:
            if self._mark:
                log.info(
                    "Оповещения: отметка %s старше границы, сдвигаем на %s",
                    self._mark, baseline,
                )
            self._mark = baseline
            self._save_state()
        log.info(
            "Оповещения: статусы %s, для канала «%s» — %s; изменения после %s "
            "(время аккаунта); %s",
            ", ".join(sorted(self._states)) or "любые",
            self._settings.opt_channel,
            ", ".join(sorted(self._opt_states)) or "любые",
            self._mark,
            self._clock.describe(),
        )
        await self._tick()

    async def _safe_baseline(self, now: Optional[datetime] = None) -> str:
        """Граница начала дня, подстрахованная от расхождения часовых поясов.

        Пояс аккаунта задаётся настройкой `MOYSKLAD_TZ`, но если её выставили
        неверно или сбились часы сервера, вычисленная граница окажется в
        «будущем» аккаунта и заказы будут молча теряться. Поэтому границу не
        поднимаем выше момента последнего изменения — но отматываем не
        больше суток, иначе тихий аккаунт привёл бы к рассылке старых
        заказов. Срабатывание пишем в лог как предупреждение.
        """
        baseline = self.baseline(now)
        newest = await self._newest_updated()
        if not newest or newest >= baseline:
            return baseline

        try:
            gap = datetime.strptime(baseline, MS_FMT) - datetime.strptime(
                newest[:19], MS_FMT
            )
        except ValueError:
            return baseline

        if gap > CLAMP_WINDOW:
            return baseline  # аккаунт просто давно без движения

        log.warning(
            "Граница %s оказалась позже последнего изменения в МойСклад, "
            "сдвигаем на %s. Проверьте MOYSKLAD_TZ и часы сервера.",
            baseline, newest[:19],
        )
        return newest[:19]

    async def _newest_updated(self) -> Optional[str]:
        payload = await self._client.get(
            "/entity/customerorder", {"order": "updated,desc", "limit": 1}
        )
        rows = payload.get("rows") or []
        return rows[0].get("updated") if rows else None

    # ------------------------------------------------------------------ опрос

    async def _tick(self) -> None:
        for order in await self._fetch_changed():
            state = ((order.get("state") or {}).get("name") or "").strip()
            key = self._key(order["id"], state)
            updated = order.get("updated")

            if key in self._seen_set:
                self._advance(updated)
                continue

            wanted = self._wanted_states(order)
            if wanted and state.lower() not in wanted:
                log.info("Заказ %s: статус «%s» вне фильтра", order.get("name"), state)
                self._remember(key)
                self._advance(updated)
                continue

            lane = routing.route(
                order, self._settings.kaspi_channel, self._settings.express_attribute
            )
            if lane == routing.BATCH:
                # Такие заказы копит сводный сборочный лист — поштучно не
                # шлём, а сразу отдаём в очередь батчера. Важно сделать это
                # здесь и сейчас: заказ может уйти из этого статуса быстрее,
                # чем батчер успеет его снова увидеть отдельным опросом.
                self._batcher.enqueue(order)
                self._remember(key)
                self._advance(updated)
                continue

            first_time = order["id"] not in self._known_orders
            card = await self._service.card_by_id(order["id"])

            attempts = self._attempts.get(key, 0) + 1
            if card.slots_failed and attempts < MAX_SLOT_ATTEMPTS:
                # Отметку времени не двигаем: иначе следующий запрос уже не
                # вернёт этот заказ и он потеряется вместе с ячейками.
                self._attempts[key] = attempts
                log.warning(
                    "Заказ %s отложен (%s/%s): %s",
                    order.get("name"), attempts, MAX_SLOT_ATTEMPTS, card.warning,
                )
                break

            self._remember(key)
            self._attempts.pop(key, None)
            await self._publish(card, state, first_time, lane)
            self._advance(updated)

        self._save_state()

    def _wanted_states(self, order: Dict[str, Any]) -> Set[str]:
        """Статусы готовности зависят от канала.

        Оптовый заказ создают пустым и наполняют позициями вручную —
        по «Новый» он ушёл бы в чат без единой строки. Готовность
        подтверждает отдельный статус.
        """
        if routing.is_opt(order, self._settings.opt_channel):
            return self._opt_states
        return self._states

    def _advance(self, updated: Optional[str]) -> None:
        if updated and (self._mark is None or updated > self._mark):
            self._mark = updated

    async def _fetch_changed(self) -> List[Dict[str, Any]]:
        """Заказы, изменившиеся не раньше отметки; дубли отсекаются по ключу."""
        payload = await self._client.get(
            "/entity/customerorder",
            {
                "filter": f"updated>={self._mark}",
                "order": "updated,asc",
                "limit": PAGE,
                "expand": "state,salesChannel,agent,store",
            },
        )
        # Отметку двигает _tick по мере успешной обработки строк: отложенный
        # заказ должен вернуться в следующей выборке.
        return payload.get("rows") or []

    async def _publish(self, card, state: str, first_time: bool, lane: str) -> None:
        if first_time:
            prefix = "🆕 <b>Новый заказ в сборку</b>"
        else:
            prefix = f"🔄 <b>Заказ перешёл в «{state}»</b>"
        if lane == routing.EXPRESS:
            prefix = "⚡️ <b>ЭКСПРЕСС</b> · " + prefix + "\nСобрать вне очереди"

        await send_card(self._bot, self._chat_id, card, self._settings,
                        prefix=prefix + "\n\n")
        log.info("Заказ %s (%s, %s) отправлен в чат %s",
                 card.number, state, lane, self._chat_id)
