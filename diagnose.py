"""Проверка связи: Telegram + МойСклад + сборка карточки последнего заказа.

    ./.venv/bin/python diagnose.py            последний заказ
    ./.venv/bin/python diagnose.py 1069127266 конкретный номер
    ./.venv/bin/python diagnose.py --notify   что ушло бы в чат склада (без отправки)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

from msbot.batcher import PickingBatcher
from msbot.config import load_settings
from msbot.formatting import render_order
from msbot.moysklad import MoySkladClient, MoySkladError
from msbot.notifier import NewOrderNotifier
from msbot.orders import OrderService


async def check_telegram(token: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"https://api.telegram.org/bot{token}/getMe")
    payload = response.json()
    if not payload.get("ok"):
        print(f"  ✗ Telegram: {payload.get('description')}")
        return
    bot = payload["result"]
    print(f"  ✓ Telegram: @{bot['username']} ({bot['first_name']}, id={bot['id']})")


class DryRunBot:
    """Заглушка вместо Telegram: копит сообщения/файлы вместо отправки."""

    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append(("сообщение", text))

    async def send_document(self, chat_id, document, caption="", **kwargs) -> None:
        self.sent.append(("файл " + getattr(document, "filename", "?"), caption))


async def preview_notifications() -> None:
    """Показывает рассылку новых заказов, ничего не отправляя в Telegram."""
    settings = load_settings()
    client = MoySkladClient(settings.moysklad_token, settings.moysklad_base_url)
    service = OrderService(client, settings.store_name, clock=settings.clock())
    bot = DryRunBot()

    with tempfile.TemporaryDirectory() as tmp:
        batcher = PickingBatcher(
            bot, service, settings,
            state_path=str(Path(tmp) / "batch_state.json"),  # боевой state не трогаем
        )
        notifier = NewOrderNotifier(
            bot, client, service, settings, batcher,
            state_path=str(Path(tmp) / "state.json"),
        )
        print(f"Чат: {settings.notify_chat_id} · отсечка: {settings.notify_since}"
              f" · статусы: {', '.join(settings.notify_states) or 'все'}")
        print(f"Граница: {notifier.baseline()}\n")
        try:
            await notifier.start_once()
            print(f"В очереди сводного листа после разбора: {len(batcher._queue)} заказов")
            # Прогоняем один круг батчера — вдруг очередь уже достаточно набралась
            await batcher.tick()
        finally:
            await client.aclose()

    if not bot.sent:
        print("Отправлять нечего: новых заказов за период нет.")
        return
    print(f"\nУшло бы: {len(bot.sent)}\n" + "-" * 60)
    for kind, text in bot.sent:
        print(f"[{kind}]")
        print(text)
        print("-" * 60)


async def main() -> None:
    settings = load_settings()

    print("Telegram")
    await check_telegram(settings.telegram_token)

    client = MoySkladClient(settings.moysklad_token, settings.moysklad_base_url)
    service = OrderService(client, settings.store_name, clock=settings.clock())

    try:
        print("\nМойСклад")
        context = await client.get("/context/employee")
        print(f"  ✓ Пользователь: {context.get('name')} <{context.get('email', '')}>")

        store = await client.find_store(settings.store_name)
        if not store:
            print(f"  ✗ Склад «{settings.store_name}» не найден. Доступные склады:")
            for row in await client.get_all_rows("/entity/store", limit=100):
                print(f"      · {row.get('name')}")
            return
        print(f"  ✓ Склад: {store['name']} (id={store['id']})")

        slots = await client.get_slots(store["id"])
        print(f"  ✓ Ячеек адресного хранения: {len(slots)}")
        for slot in slots[:5]:
            print(f"      · {slot.get('name')}")

        number = sys.argv[1] if len(sys.argv) > 1 else None
        if number:
            card = await service.card_by_number(number)
            if card is None:
                print(f"\n  ✗ Заказ {number} не найден")
                return
        else:
            payload = await client.get(
                "/entity/customerorder",
                {"order": "moment,desc", "limit": 1, "expand": "state,salesChannel,agent,store"},
            )
            rows = payload.get("rows") or []
            if not rows:
                print("\n  ! Заказов покупателя в аккаунте нет — проверить карточку не на чем")
                return
            card = await service.card_by_id(rows[0]["id"])

        print("\nКарточка заказа (как её увидит кладовщик)")
        print("-" * 60)
        for chunk in render_order(card, settings.compact_slots, settings.table_width):
            print(chunk)
        print("-" * 60)

        with_slots = sum(1 for p in card.positions if p.slots)
        print(f"Позиций: {len(card.positions)}, из них с ячейками: {with_slots}")

    except MoySkladError as exc:
        print(f"  ✗ {exc}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    if "--notify" in sys.argv:
        asyncio.run(preview_notifications())
    else:
        asyncio.run(main())
