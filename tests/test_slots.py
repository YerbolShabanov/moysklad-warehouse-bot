"""Выбор ячейки: сначала нижние этажи, третий — только когда иначе никак."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msbot import slots
from msbot.formatting import cell_label
from msbot.orders import OrderService
from test_smoke import BALL_ID, STICK_ID, FakeClient


def test_parsing():
    assert slots.parse("Ряд 10 место 9 этаж 2") == (10, 9, 2)
    assert slots.parse("ряд 1 место 1 этаж 1") == (1, 1, 1)
    assert slots.parse("Приёмка 1") is None
    assert slots.parse("") is None

    assert slots.compact("Ряд 10 место 9 этаж 2") == "Р10-М9-Э2"
    assert slots.compact("Комплектация стол 2") == "Комплектация стол 2"

    assert slots.floor("Ряд 3 место 4 этаж 3") == 3
    assert slots.floor("Приёмка 1") is None

    # Этаж 1 лучше 2, 2 лучше напольной ячейки, напольная лучше третьего:
    # на третьем упакованные паллеты, их снимать погрузчиком.
    assert slots.pick_rank("Ряд 1 место 1 этаж 1") == 1
    assert slots.pick_rank("Ряд 1 место 1 этаж 2") == 2
    assert slots.pick_rank("Приёмка 1") == 2.5
    assert slots.pick_rank("Ряд 1 место 1 этаж 3") == 3

    # Маршрут обхода — числовой, не алфавитный
    assert slots.walk_key("Ряд 2 место 1 этаж 1") < slots.walk_key("Ряд 10 место 1 этаж 1")
    assert slots.walk_key("Ряд 10 место 3 этаж 1") < slots.walk_key("Ряд 10 место 9 этаж 1")
    assert slots.walk_key("Ряд 99 место 9 этаж 9") < slots.walk_key("Приёмка 1")


class StockClient(FakeClient):
    """Остатки по ячейкам задаются тестом."""

    ball_quantity = 5.0

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def get_order_positions(self, order_id):
        rows = await super().get_order_positions(order_id)
        rows[0]["quantity"] = self.ball_quantity
        return rows

    async def get_slots(self, store_id):
        names = {r["slotId"] for r in self.rows}
        return [{"id": n, "name": n} for n in names]

    async def stock_by_slot(self, store_id, assortment_ids, chunk_size=50):
        return [dict(r, storeId=store_id) for r in self.rows]


async def position_for(rows, needed=None):
    """Позиция «мяча» после подстановки ячеек; needed — требуемое количество."""
    client = StockClient(rows)
    if needed is not None:
        client.ball_quantity = needed
    service = OrderService(client, "Склад адрес. хранение")
    card = await service.card_by_number("00123")
    return card.positions[0]


async def chosen(rows, needed=None):
    """Возвращает ячейку, которую бот подставит в заказ для «мяча»."""
    ball = await position_for(rows, needed)
    return ball.slots[0].slot_name, [s.slot_name for s in ball.slots]


def row(slot, stock):
    return {"assortmentId": BALL_ID, "slotId": slot, "stock": stock}


async def main():
    test_parsing()

    # Во всех сценариях 1-6 количества хватает в любой ячейке: они проверяют
    # именно приоритет этажей. Достаточность — сценарии 7-10.

    # 1. Есть первый этаж — берём его, даже если остаток там меньше
    pick, order = await chosen([
        row("Ряд 5 место 2 этаж 3", 100),
        row("Ряд 7 место 1 этаж 2", 50),
        row("Ряд 9 место 4 этаж 1", 6),
    ])
    assert pick == "Ряд 9 место 4 этаж 1", pick
    assert order == [
        "Ряд 9 место 4 этаж 1",
        "Ряд 7 место 1 этаж 2",
        "Ряд 5 место 2 этаж 3",
    ], order

    # 2. Первого этажа нет — уходим на второй, а не на третий
    pick, _ = await chosen([
        row("Ряд 5 место 2 этаж 3", 100),
        row("Ряд 7 место 1 этаж 2", 6),
    ])
    assert pick == "Ряд 7 место 1 этаж 2", pick

    # 3. Только третий этаж — берём его, деваться некуда
    pick, _ = await chosen([row("Ряд 5 место 2 этаж 3", 4)])
    assert pick == "Ряд 5 место 2 этаж 3", pick

    # 4. На одном этаже несколько ячеек — берём где больше остаток
    pick, _ = await chosen([
        row("Ряд 2 место 1 этаж 1", 3),
        row("Ряд 8 место 6 этаж 1", 11),
        row("Ряд 4 место 5 этаж 1", 7),
    ])
    assert pick == "Ряд 8 место 6 этаж 1", pick

    # 5. Напольная ячейка предпочтительнее третьего этажа
    pick, _ = await chosen([
        row("Ряд 5 место 2 этаж 3", 100),
        row("Приёмка 1", 6),
    ])
    assert pick == "Приёмка 1", pick

    # 6. ...но второй этаж предпочтительнее напольной
    pick, _ = await chosen([
        row("Приёмка 1", 100),
        row("Ряд 6 место 6 этаж 2", 6),
    ])
    assert pick == "Ряд 6 место 6 этаж 2", pick

    # 7. Количества не хватает на нижнем этаже — идём туда, где хватает,
    #    даже если это третий этаж. Приоритет этажей действует уже внутри
    #    достаточных ячеек.
    ball = await position_for([
        row("Ряд 3 место 6 этаж 2", 1),
        row("Ряд 2 место 2 этаж 3", 8),
    ], needed=2)
    assert ball.slots[0].slot_name == "Ряд 2 место 2 этаж 3", ball.slots[0]
    assert ball.enough is True

    # 8. Среди достаточных по-прежнему выигрывает нижний этаж
    ball = await position_for([
        row("Ряд 2 место 2 этаж 3", 50),
        row("Ряд 5 место 1 этаж 1", 2),
        row("Ряд 7 место 3 этаж 2", 30),
    ], needed=2)
    assert ball.slots[0].slot_name == "Ряд 5 место 1 этаж 1", ball.slots[0]

    # 9. Достаточной ячейки нет вовсе — берём лучшую и помечаем нехватку
    ball = await position_for([
        row("Ряд 9 место 9 этаж 3", 3),
        row("Ряд 1 место 1 этаж 1", 1),
    ], needed=10)
    assert ball.slots[0].slot_name == "Ряд 1 место 1 этаж 1", ball.slots[0]
    assert ball.enough is False
    assert cell_label(ball) == "Р1-М1-Э1 (есть 1 из 10)", cell_label(ball)

    # 10. Когда хватает — подпись без оговорок
    ball = await position_for([row("Ряд 1 место 1 этаж 1", 10)], needed=10)
    assert ball.enough is True
    assert cell_label(ball) == "Р1-М1-Э1"

    print("OK: ячейка выбирается по достаточности, затем снизу вверх")


if __name__ == "__main__":
    asyncio.run(main())
