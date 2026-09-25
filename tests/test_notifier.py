"""Оповещения: отслеживание смены статуса, отсечка по дню, защита от дублей."""
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msbot.config import Settings
from msbot.moysklad import MoySkladError
from msbot.notifier import MAX_SLOT_ATTEMPTS, NewOrderNotifier
from msbot.orders import OrderService
from test_smoke import BALL_ID, FakeClient

# Привязываемся к сегодняшнему дню: start() без аргумента берёт реальные
# часы, и фиксированная дата ломала бы тест на следующие сутки.
NOW = datetime.now().replace(hour=14, minute=30, second=0, microsecond=0)
TODAY = NOW.strftime("%Y-%m-%d")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")


def order(oid, name, updated, state="Упаковка", created=None):
    return {"id": oid, "name": name, "created": created or updated,
            "updated": updated, "moment": updated, "state": {"name": state}}


class NotifierClient(FakeClient):
    """Лента заказов, которую тест меняет по ходу — как это делает МойСклад."""

    def __init__(self, orders):
        super().__init__()
        self.orders = {o["id"]: dict(o) for o in orders}

    def touch(self, oid, state, updated):
        """Имитирует смену статуса в МойСклад."""
        self.orders[oid].update(state={"name": state}, updated=updated)

    async def get(self, path, params=None):
        params = params or {}
        assert path == "/entity/customerorder", path
        rows = sorted(self.orders.values(), key=lambda o: o["updated"])
        if params.get("order") == "updated,desc":
            return {"rows": [rows[-1]] if rows else []}
        since = (params.get("filter") or "updated>=").split("updated>=", 1)[1]
        return {"rows": [o for o in rows if o["updated"] >= since]}

    async def get_order(self, order_id):
        found = self.orders[order_id]
        return dict(found, salesChannel={"name": "Kaspi"}, agent={"name": "К"},
                    store={"name": "Склад адрес. хранение"})


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id, document, caption="", **kwargs):
        self.sent.append((chat_id, caption))

    def numbers(self):
        return set(re.findall(r"Заказ покупателя (\d+)", "\n".join(t for _, t in self.sent)))

    def text(self):
        return "\n".join(t for _, t in self.sent)


def make(tmp, client, bot, states=None, since="today", opt_states=None):
    settings = Settings(
        telegram_token="t", moysklad_token="m",
        notify_chat_id=-100123, poll_interval=15,
        notify_states=list(states or []), notify_since=since,
        order_format="text", opt_states=list(opt_states or ["Моя Доставка"]),
    )
    return NewOrderNotifier(
        bot, client, OrderService(client, "Склад адрес. хранение"), settings,
        state_path=str(Path(tmp) / "state.json"),
    )


async def notifier_start_then_stop(notifier):
    task = asyncio.ensure_future(notifier.start())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if notifier._bot.sent:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def main():
    # 1. Граница отсчёта. Считается по часам склада (Алматы), а в фильтр
    #    уходит в поясе аккаунта (Москва) — на два часа раньше.
    with tempfile.TemporaryDirectory() as tmp:
        n = make(tmp, None, None)
        assert n.baseline(NOW) == f"{YESTERDAY} 22:00:00", n.baseline(NOW)
        assert make(tmp, None, None, since="now").baseline(NOW) == f"{TODAY} 12:30:00"
        explicit = f"{YESTERDAY} 06:00:00"  # явную дату считаем уже московской
        assert make(tmp, None, None, since=explicit).baseline(NOW) == explicit

    # 2. Заказ, созданный в «Упаковка», доезжает — фильтр смотрит текущий статус
    with tempfile.TemporaryDirectory() as tmp:
        client = NotifierClient([
            order("o1", "1001", f"{YESTERDAY} 09:00:00.000"),
            order("o2", "1002", f"{TODAY} 08:55:00.000"),
        ])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка", "Передача"])
        await notifier.start_once(NOW)
        assert bot.numbers() == {"1002"}, bot.numbers()
        assert "Новый заказ в сборку" in bot.text()

        # 3. Тот же статус второй раз не шлём
        bot.sent.clear()
        await notifier._tick()
        assert bot.sent == [], bot.sent

        # 4. Переход «Упаковка» -> «Передача» — это новое событие
        client.touch("o2", "Передача", f"{TODAY} 11:00:00.000")
        await notifier._tick()
        assert bot.numbers() == {"1002"}, bot.numbers()
        assert "перешёл в «Передача»" in bot.text(), bot.text()

        # 5. Переход в статус вне фильтра — тишина
        bot.sent.clear()
        client.touch("o2", "Выдан", f"{TODAY} 12:00:00.000")
        await notifier._tick()
        assert bot.sent == [], bot.sent

        # 6. Возврат в тот же статус повторно НЕ шлём: мигание статусов
        #    в МойСклад не должно засыпать чат одинаковыми файлами
        client.touch("o2", "Передача", f"{TODAY} 12:30:00.000")
        await notifier._tick()
        assert bot.sent == [], bot.sent

    # 7. Перезапуск не дублирует уже отправленное
    with tempfile.TemporaryDirectory() as tmp:
        client = NotifierClient([order("o3", "1003", f"{TODAY} 09:00:00.000")])
        first = make(tmp, client, FakeBot(), states=["Упаковка"])
        await first.start_once(NOW)
        assert first._bot.numbers() == {"1003"}

        saved = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
        assert saved["last_updated"] == f"{TODAY} 09:00:00.000"
        assert "o3:упаковка" in saved["seen"], saved["seen"]

        again = make(tmp, client, FakeBot(), states=["Упаковка"])
        await again.start_once(NOW)
        assert again._bot.sent == [], "после перезапуска заказ ушёл повторно"

    # 8. Состояние прежней версии (last_created) читается без потери отметки
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "state.json").write_text(json.dumps({
            "last_created": f"{TODAY} 10:00:00.000", "seen": ["o3"],
        }), encoding="utf-8")
        client = NotifierClient([order("o4", "1004", f"{TODAY} 09:00:00.000")])
        notifier = make(tmp, client, FakeBot(), states=["Упаковка"])
        await notifier.start_once(NOW)
        # заказ изменён раньше отметки — не всплывает
        assert notifier._bot.sent == [], notifier._bot.sent

    # 9. Бот молчал неделю: вчерашние изменения не всплывают
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "state.json").write_text(json.dumps({
            "last_updated": f"{(NOW - timedelta(days=7)).strftime('%Y-%m-%d')} 10:00:00.000",
            "seen": [],
        }), encoding="utf-8")
        client = NotifierClient([
            order("o5", "1005", f"{YESTERDAY} 09:00:00.000"),
            order("o6", "1006", f"{TODAY} 09:00:00.000"),
        ])
        notifier = make(tmp, client, FakeBot(), states=["Упаковка"])
        await notifier.start_once(NOW)
        assert notifier._bot.numbers() == {"1006"}, notifier._bot.numbers()

    # 10. Часы сервера убежали вперёд аккаунта — ночные заказы не теряются
    with tempfile.TemporaryDirectory() as tmp:
        client = NotifierClient([order("o7", "1007", f"{YESTERDAY} 22:30:00.000")])
        notifier = make(tmp, client, FakeBot(), states=["Упаковка"])
        await notifier.start_once(NOW.replace(hour=0, minute=30))
        assert notifier._bot.numbers() == {"1007"}, notifier._bot.numbers()

    # 11. Тихий аккаунт: старое всё равно не рассылается
    with tempfile.TemporaryDirectory() as tmp:
        stale = order("o8", "1008",
                      f"{(NOW - timedelta(days=5)).strftime('%Y-%m-%d')} 10:00:00.000")
        notifier = make(tmp, NotifierClient([stale]), FakeBot(), states=["Упаковка"])
        await notifier.start_once(NOW)
        assert notifier._bot.sent == [], notifier._bot.sent

    # 12. МойСклад недоступен на старте — рассылка ждёт, а не умирает
    with tempfile.TemporaryDirectory() as tmp:
        class FlakyClient(NotifierClient):
            fails = 2

            async def get(self, path, params=None):
                if self.fails:
                    self.fails -= 1
                    raise MoySkladError("МойСклад 401: просроченный ключ доступа")
                return await super().get(path, params)

        client = FlakyClient([order("o9", "1009", f"{TODAY} 09:00:00.000")])
        notifier = make(tmp, client, FakeBot(), states=["Упаковка"])
        notifier._interval = 0
        await asyncio.wait_for(notifier_start_then_stop(notifier), timeout=5)
        assert client.fails == 0, "клиент не был опрошен повторно"
        assert notifier._bot.numbers() == {"1009"}, notifier._bot.numbers()

    # 13. Отчёт по ячейкам лежит: заказ откладывается, а не уходит без адресов
    with tempfile.TemporaryDirectory() as tmp:
        class NoSlotsClient(NotifierClient):
            broken = True

            async def stock_by_slot(self, store_id, assortment_ids, chunk_size=50):
                if self.broken:
                    raise MoySkladError("МойСклад 503: сервис временно недоступен")
                return [{"assortmentId": BALL_ID, "storeId": store_id,
                         "slotId": "slot-a", "stock": 5}]

        client = NoSlotsClient([order("oA", "2001", f"{TODAY} 09:00:00.000")])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка"])
        await notifier.start_once(NOW)
        assert bot.sent == [], "карточка без ячеек ушла в чат"

        # отметка не сдвинулась — иначе заказ выпал бы из следующей выборки
        assert notifier._mark < f"{TODAY} 09:00:00.000", notifier._mark

        # отчёт ожил — заказ доезжает, уже с ячейкой
        client.broken = False
        await notifier._tick()
        assert bot.numbers() == {"2001"}, bot.numbers()
        assert "Р10-М9-Э2" in bot.text() or "Ряд 10" in bot.text(), bot.text()

    # 14. Если отчёт не оживает, заказ всё равно отправляется — с оговоркой
    with tempfile.TemporaryDirectory() as tmp:
        client = NoSlotsClient([order("oB", "2002", f"{TODAY} 09:00:00.000")])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка"])
        await notifier.start_once(NOW)
        for _ in range(MAX_SLOT_ATTEMPTS):
            if bot.sent:
                break
            await notifier._tick()
        assert bot.numbers() == {"2002"}, bot.numbers()
        assert "Ячейки не получены" in bot.text(), bot.text()

    # 15. Маршрутизация: Kaspi без экспресса поштучно не уходит,
    #     экспресс уходит с пометкой, ОПТ — как раньше
    with tempfile.TemporaryDirectory() as tmp:
        kaspi = dict(order("oK", "3001", f"{TODAY} 09:00:00.000"),
                     salesChannel={"name": "Kaspi магазин"},
                     agent={"name": "Kaspi магазин"})
        express = dict(order("oE", "3002", f"{TODAY} 09:01:00.000"),
                       salesChannel={"name": "Kaspi магазин"},
                       agent={"name": "Kaspi магазин"},
                       attributes=[{"name": "Экспресс доставка", "value": True}])
        # ОПТ отдаём по своему статусу готовности, а не по «Упаковка»
        opt = dict(order("oO", "3003", f"{TODAY} 09:02:00.000", state="Моя Доставка"),
                   salesChannel={"name": "ОПТ"}, agent={"name": "Карабаликов ИП"})

        class RoutingClient(NotifierClient):
            async def get_order(self, order_id):
                return dict(self.orders[order_id], store={"name": "Склад"})

        client = RoutingClient([kaspi, express, opt])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка"])
        await notifier.start_once(NOW)

        assert bot.numbers() == {"3002", "3003"}, bot.numbers()
        assert "3001" not in bot.text(), "заказ Kaspi ушёл поштучно вместо сводного листа"
        assert "ЭКСПРЕСС" in bot.text(), bot.text()
        assert "Собрать вне очереди" in bot.text(), bot.text()
        # пометка стоит только на экспрессе
        express_chunk = next(t for _, t in bot.sent if "3002" in t)
        opt_chunk = next(t for _, t in bot.sent if "3003" in t)
        assert "ЭКСПРЕСС" in express_chunk and "ЭКСПРЕСС" not in opt_chunk

    # 16. ОПТ: по «Новый» не отдаём — заказ ещё наполняют позициями вручную,
    #     уйдёт пустой файл. Ждём статус готовности к отгрузке.
    with tempfile.TemporaryDirectory() as tmp:
        opt = dict(order("oP", "4001", f"{TODAY} 08:46:00.000", state="Новый"),
                   salesChannel={"name": "ОПТ"}, agent={"name": "ТОО Ути Пути"})

        class OptClient(NotifierClient):
            async def get_order(self, order_id):
                return dict(self.orders[order_id], store={"name": "Склад"})

        client = OptClient([opt])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка", "Новый"])
        await notifier.start_once(NOW)
        assert bot.sent == [], "пустой оптовый заказ ушёл в чат"

        # позиции добавили, заказ подтвердили — теперь отдаём
        client.touch("oP", "Моя Доставка", f"{TODAY} 09:14:00.000")
        await notifier._tick()
        assert bot.numbers() == {"4001"}, bot.numbers()
        assert "ЭКСПРЕСС" not in bot.text()

        # промежуточные статусы оптового заказа в чат не идут
        bot.sent.clear()
        client.touch("oP", "Упаковка", f"{TODAY} 09:30:00.000")
        await notifier._tick()
        assert bot.sent == [], "ОПТ ушёл по статусу маркетплейса"

    # 17. Для Kaspi статусы прежние — правило для ОПТ их не задело
    with tempfile.TemporaryDirectory() as tmp:
        kaspi = dict(order("oK2", "4002", f"{TODAY} 09:00:00.000"),
                     salesChannel={"name": "Kaspi магазин"},
                     agent={"name": "Kaspi магазин"},
                     attributes=[{"name": "Экспресс доставка", "value": True}])
        client = OptClient([kaspi])
        bot = FakeBot()
        notifier = make(tmp, client, bot, states=["Упаковка", "Новый"])
        await notifier.start_once(NOW)
        assert bot.numbers() == {"4002"}, bot.numbers()

    print("OK: ловятся смены статуса, маршруты разведены, ОПТ ждёт готовности")


if __name__ == "__main__":
    asyncio.run(main())
