"""Проверка сборки карточки заказа на подставном API МойСклад."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msbot.formatting import _fit, compact_slot, render_order
from msbot.orders import OrderCard, OrderPosition, OrderService, SlotStock

STORE_ID = "11111111-0000-0000-0000-000000000001"
BALL_ID = "22222222-0000-0000-0000-000000000001"
STICK_ID = "22222222-0000-0000-0000-000000000002"


class FakeClient:
    """Повторяет контракт MoySkladClient, отдаёт фиксированные ответы."""

    def __init__(self):
        self.calls = []

    async def find_store(self, name):
        self.calls.append(("find_store", name))
        return {"id": STORE_ID, "name": name}

    async def get_slots(self, store_id):
        self.calls.append(("get_slots", store_id))
        return [
            {"id": "slot-a", "name": "Ряд 10 место 9 этаж 2"},
            {"id": "slot-b", "name": "Приёмка 1"},
        ]

    async def find_order_by_name(self, name):
        if name != "00123":
            return None
        return {
            "id": "order-1",
            "name": "00123",
            "moment": "2026-09-05 14:30:00.000",
            "state": {"name": "Новый"},
            "salesChannel": {"name": "Ozon"},
            "agent": {"name": 'ООО "Ромашка"'},
            "store": {"name": "Склад адрес. хранение"},
        }

    async def get_order(self, order_id):
        return await self.find_order_by_name("00123")

    async def get_order_positions(self, order_id):
        return [
            {
                "quantity": 5.0,
                "reserve": 5.0,
                "assortment": {
                    "id": BALL_ID,
                    "name": "Мяч футбольный",
                    "article": "AR-23",
                    "code": "00003",
                    "barcodes": [{"ean13": "4690624257353"}, {"code128": "X"}],
                    "uom": {"name": "шт"},
                },
            },
            {
                "quantity": 2.5,
                "assortment": {
                    "id": STICK_ID,
                    "name": "Клюшка хоккейная <спорт>",
                    "code": "00007",
                    "uom": {"name": "кг"},
                },
            },
        ]

    async def stock_by_slot(self, store_id, assortment_ids, chunk_size=50):
        self.calls.append(("stock_by_slot", store_id, tuple(assortment_ids)))
        return [
            {"assortmentId": BALL_ID, "storeId": store_id, "slotId": "slot-b", "stock": 3.0},
            {"assortmentId": BALL_ID, "storeId": store_id, "slotId": "slot-a", "stock": 12.0},
        ]


async def main():
    client = FakeClient()
    service = OrderService(client, "Склад адрес. хранение")

    card = await service.card_by_number("00123")
    assert card is not None
    assert card.number == "00123"
    assert card.state == "Новый"
    assert card.sales_channel == "Ozon"
    # МойСклад отдаёт 14:30 по Москве — складу показываем 16:30 по Алматы
    assert card.moment.strftime("%d.%m.%Y %H:%M") == "05.09.2026 16:30", card.moment
    assert card.store == "Склад адрес. хранение"

    ball, stick = card.positions
    assert ball.quantity == 5.0 and ball.uom == "шт"
    # Первый штрихкод из карточки товара, а не артикул
    assert ball.barcode == "4690624257353"
    assert stick.barcode == ""  # штрихкодов нет -> в таблице подставится код
    # Ячейки отсортированы по убыванию остатка: первой идёт самая полная
    assert [(s.slot_name, s.stock) for s in ball.slots] == [
        ("Ряд 10 место 9 этаж 2", 12.0),
        ("Приёмка 1", 3.0),
    ]
    assert stick.slots == []

    # Сокращение имён ячеек
    assert compact_slot("Ряд 10 место 9 этаж 2") == "Р10-М9-Э2"
    assert compact_slot("Приёмка 1") == "Приёмка 1"

    messages = render_order(card)
    text = "\n".join(messages)
    assert "Заказ покупателя 00123" in text
    assert "Канал продаж: Ozon" in text

    table = text[text.index("<pre>") + 5:text.index("</pre>")]
    rows = table.splitlines()
    assert rows[0].split() == ["Наименование", "Штрихкод", "Кол", "Ед", "Ячейка"], rows[0]
    assert rows[1].split() == ["Мяч", "футбольный", "4690624257353", "5", "шт", "Р10-М9-Э2"], rows[1]
    # Нумерация позиций убрана
    assert not rows[1].startswith("1 ")
    # Вторая ячейка того же товара в таблицу не попадает
    assert "Приёмка" not in table
    # Позиция без ячейки — прочерк
    assert rows[2].split()[-1] == "—", rows[2]
    # Таблица укладывается в лимит ширины: Telegram переносит длинные строки
    assert max(len(line) for line in rows) <= 58, max(len(line) for line in rows)
    # Колонки выровнены по заголовку
    assert rows[1].index("4690624257353") == rows[0].index("Штрихкод")
    # Названия теперь внутри таблицы, отдельного списка нет
    assert "<b>1</b> —" not in text
    assert "&lt;спорт&gt;" in table
    assert all(len(m) <= 4096 for m in messages)

    # Короткий заказ — одно сообщение
    assert len(messages) == 1, messages

    # Порядок обхода: по ряду, месту и этажу; без ячейки — в конец
    def slotted(name, row, place, level):
        p = OrderPosition(name=name, code="", barcode="", quantity=1, uom="шт")
        p.slots = [SlotStock(slot_name=f"Ряд {row} место {place} этаж {level}", stock=1)]
        return p

    mixed = [
        slotted("Г", 10, 1, 1),
        slotted("Б", 2, 5, 1),
        slotted("В", 2, 5, 3),
        OrderPosition(name="Д", code="", barcode="", quantity=1, uom="шт"),
        slotted("А", 2, 1, 1),
    ]
    picking = OrderCard(id="x", number="1", state="Новый", moment=None,
                        sales_channel="—", agent="", store="", positions=mixed)
    rendered = "\n".join(render_order(picking))
    body = rendered[rendered.index("<pre>") + 5:rendered.index("</pre>")].splitlines()
    names = [line.split()[0] for line in body[1:]]
    assert names == ["А", "Б", "В", "Г", "Д"], names

    # Длинное название режется по середине: артикул и размер остаются видны
    long_name = "86768, Сапоги резиновые детские (серо-голубой, 23 (15,8)_26)"
    fitted = _fit(long_name, 40)
    assert len(fitted) == 40 and "…" in fitted, fitted
    assert fitted.startswith("86768,") and fitted.endswith("(15,8)_26)"), fitted
    assert _fit(long_name, 0) == long_name

    # Кэш склада и ячеек: повторный запрос не ходит в API заново
    await service.card_by_number("00123")
    assert [c[0] for c in client.calls].count("find_store") == 1
    assert [c[0] for c in client.calls].count("get_slots") == 1

    assert await service.card_by_number("99999") is None

    # Длинный заказ режется на несколько сообщений, каждое — валидный <pre>
    card.positions = card.positions * 200
    long_messages = render_order(card)
    assert len(long_messages) > 1
    assert all(len(m) <= 4096 for m in long_messages)
    for message in long_messages:
        assert message.count("<pre>") == message.count("</pre>")
    # Заголовок таблицы повторяется в каждом куске таблицы
    table_chunks = [m for m in long_messages if "<pre>" in m]
    assert all("Штрихкод" in m for m in table_chunks)

    print("OK: все проверки пройдены,", len(long_messages), "сообщений в длинном заказе")


if __name__ == "__main__":
    asyncio.run(main())
