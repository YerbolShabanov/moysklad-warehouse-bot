"""Маршрутизация заказов и сводный сборочный лист."""
import asyncio
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

from msbot import routing
from msbot.batcher import PickingBatcher
from msbot.config import Settings
from msbot.excel import render_batch_xlsx
from msbot.orders import OrderCard, OrderPosition, OrderService, SlotStock
from test_smoke import FakeClient

BASE = "https://api.moysklad.ru/api/remap/1.2"
KASPI = {"salesChannel": {"name": "Kaspi магазин"}, "agent": {"name": "Kaspi магазин"}}
EXPRESS_ON = [{"name": "Экспресс доставка", "value": True}]


def test_routing():
    assert routing.route(KASPI, "Kaspi магазин", "Экспресс доставка") == routing.BATCH
    express = dict(KASPI, attributes=EXPRESS_ON)
    assert routing.route(express, "Kaspi магазин", "Экспресс доставка") == routing.EXPRESS
    # Снятая галочка приходит как отсутствие атрибута, а не как false
    off = dict(KASPI, attributes=[{"name": "Тип доставки", "value": "Kaspi Postomat"}])
    assert routing.route(off, "Kaspi магазин", "Экспресс доставка") == routing.BATCH
    assert routing.is_express(dict(KASPI, attributes=[{"name": "Экспресс доставка",
                                                       "value": False}]),
                              "Экспресс доставка") is False

    opt = {"salesChannel": {"name": "ОПТ"}, "agent": {"name": "Карабаликов ИП"}}
    assert routing.route(opt, "Kaspi магазин", "Экспресс доставка") == routing.INDIVIDUAL
    # Совпадения только по контрагенту достаточно
    by_agent = {"salesChannel": {"name": "Точка продаж"}, "agent": {"name": "Kaspi магазин"}}
    assert routing.route(by_agent, "Kaspi магазин", "Экспресс доставка") == routing.BATCH


def card(number, positions, state="Упаковка"):
    return OrderCard(id=f"id-{number}", number=number, state=state,
                     moment=datetime.now(), sales_channel="Kaspi магазин",
                     agent="Kaspi магазин", store="Склад", positions=positions)


def pos(name, barcode, row, place, level, qty=1):
    p = OrderPosition(name=name, code="", barcode=barcode, quantity=qty, uom="шт")
    p.slots = [SlotStock(slot_name=f"Ряд {row} место {place} этаж {level}", stock=9)]
    return p


def test_sheet():
    cards = [
        card("1001", [pos("Автокресло OXIS", "4690624238147", 10, 2, 1),
                      pos("Ходунки SMILEY", "0004690624101601", 3, 4, 1)]),
        card("1002", [pos("Беговел WALLY", "4690624197727", 3, 1, 1, qty=2)], state="Новый"),
    ]
    book = load_workbook(io.BytesIO(render_batch_xlsx(cards)))
    assert book.sheetnames == ["Маршрут сборки", "По заказам"], book.sheetnames

    route = book["Маршрут сборки"]
    head_row = next(r for r in range(1, route.max_row + 1)
                    if route.cell(row=r, column=1).value == "№")
    head = [route.cell(row=head_row, column=c).value for c in range(1, 7)]
    assert head == ["№", "Наименование товара", "Штрихкод", "Кол-во", "Ячейка", "Заказ"], head

    body = [[route.cell(row=r, column=c).value for c in range(1, 7)]
            for r in range(head_row + 1, route.max_row + 1)]
    # Маршрут не зависит от заказа: идём по рядам подряд
    assert [r[4] for r in body] == ["Р3-М1-Э1", "Р3-М4-Э1", "Р10-М2-Э1"], body
    # У каждой строки виден свой заказ
    assert [r[5] for r in body] == ["1002", "1001", "1001"], body
    # Штрихкод текстом — ведущий ноль на месте
    assert body[1][2] == "0004690624101601", body[1]

    orders = book["По заказам"]
    ohead = next(r for r in range(1, orders.max_row + 1)
                 if orders.cell(row=r, column=1).value == "Заказ")
    numbers = [orders.cell(row=r, column=1).value
               for r in range(ohead + 1, orders.max_row + 1)]
    # Номер печатается один раз на группу, дальше пусто (openpyxl отдаёт None)
    assert numbers == ["1001", None, "1002"], numbers


class PoolClient(FakeClient):
    """Пул заказов Kaspi, как его отдаёт МойСклад по фильтру."""

    base_url = BASE

    def __init__(self, orders):
        super().__init__()
        self.orders = list(orders)
        self.filters = []

    async def get(self, path, params=None):
        params = params or {}
        if path == "/entity/customerorder/metadata":
            return {"states": [{"id": "s-new", "name": "Новый"},
                               {"id": "s-pack", "name": "Упаковка"},
                               {"id": "s-give", "name": "Выдан"}]}
        if path == "/entity/saleschannel":
            return {"rows": [{"id": "ch-kaspi", "name": "Kaspi магазин"}]}
        if path == "/entity/customerorder/metadata/attributes":
            return {"rows": [{"id": "attr-exp", "name": "Экспресс доставка"}]}
        if path == "/entity/customerorder":
            self.filters.append(params.get("filter"))
            return {"rows": sorted(self.orders, key=lambda o: o["created"])}
        raise AssertionError(path)

    async def get_order(self, order_id):
        found = next(o for o in self.orders if o["id"] == order_id)
        return dict(found, salesChannel={"name": "Kaspi магазин"},
                    agent={"name": "Kaspi магазин"}, store={"name": "Склад"})


class FakeBot:
    def __init__(self):
        self.documents = []

    async def send_document(self, chat_id, document, caption="", **kwargs):
        self.documents.append((chat_id, document, caption))


def order_row(oid, number, created):
    return {"id": oid, "name": number, "created": created, "moment": created,
            "state": {"name": "Упаковка"}}


def make(tmp, client, bot, size=10, wait=60):
    settings = Settings(
        telegram_token="t", moysklad_token="m", notify_chat_id=-100500,
        notify_states=["Новый", "Упаковка"], batch_size=size, batch_max_wait=wait,
    )
    return PickingBatcher(
        bot, client, OrderService(client, "Склад адрес. хранение", clock=settings.clock()),
        settings, state_path=str(Path(tmp) / "batch.json"),
    )


def minutes_ago(clock, value):
    moment = clock.now_display() - timedelta(minutes=value)
    return moment.astimezone(clock.moysklad).strftime("%Y-%m-%d %H:%M:%S.000")


async def main():
    test_routing()
    test_sheet()

    # 1. Меньше порога и ждали недолго — лист не уходит
    with tempfile.TemporaryDirectory() as tmp:
        bot = FakeBot()
        batcher = make(tmp, PoolClient([]), bot, size=3, wait=60)
        clock = batcher._clock
        client = PoolClient([order_row("o1", "1001", minutes_ago(clock, 5)),
                             order_row("o2", "1002", minutes_ago(clock, 3))])
        batcher._client = client
        batcher._service = OrderService(client, "Склад адрес. хранение", clock=clock)
        assert await batcher.tick() is None
        assert bot.documents == []

        # фильтр собран из статусов, канала и доп. поля
        flt = client.filters[-1]
        assert "state=" in flt and "s-new" in flt and "s-pack" in flt, flt
        assert "s-give" not in flt, "лишний статус попал в фильтр"
        assert "salesChannel=" in flt and "ch-kaspi" in flt, flt
        assert flt.endswith("attributes/attr-exp=false"), flt

        # 2. Набрался порог — уходит ровно size заказов, самые старые
        client.orders.append(order_row("o3", "1003", minutes_ago(clock, 1)))
        client.orders.append(order_row("o4", "1004", minutes_ago(clock, 0)))
        assert await batcher.tick() == 3
        assert len(bot.documents) == 1
        chat, document, caption = bot.documents[0]
        assert chat == -100500
        assert document.filename.startswith("Сборочный-лист-3-зак-")
        assert "Заказов: <b>3</b>" in caption, caption
        assert "1001, 1002, 1003" in caption and "1004" not in caption, caption

        # 3. Повторный круг не отправляет те же заказы
        assert await batcher.tick() is None

        # 4. Ожидание вышло — уходит остаток, даже если порог не набран
        client.orders.append(order_row("o5", "1005", minutes_ago(clock, 90)))
        assert await batcher.tick() == 2
        assert len(bot.documents) == 2

        # 5. Отправленное переживает перезапуск
        saved = json.loads((Path(tmp) / "batch.json").read_text(encoding="utf-8"))
        assert set(saved["sent"]) == {"o1", "o2", "o3", "o4", "o5"}, saved
        again = make(tmp, client, FakeBot(), size=3, wait=60)
        again._client, again._service = client, batcher._service
        again._load_state()
        assert await again.tick() is None
        assert again._bot.documents == []

    print("OK: маршрутизация разводит заказы, сводный лист копится и не дублируется")


if __name__ == "__main__":
    asyncio.run(main())
