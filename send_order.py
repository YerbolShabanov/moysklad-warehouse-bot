"""Ручная отправка карточки заказа в чат склада.

    ./.venv/bin/python send_order.py 1069566492 1068634272
    ./.venv/bin/python send_order.py --dry 1069566492    только проверить, не отправляя
    ./.venv/bin/python send_order.py --save ./out 1068634272   сохранить .xlsx локально

Формат берётся из ORDER_FORMAT (.env): xlsx, text или image.

Шлёт обычную карточку — без плашки «Новый заказ», чтобы повтор не выглядел
как только что поступивший заказ. `state.json` не трогает.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from msbot.config import load_settings
from msbot.delivery import send_card
from msbot.excel import order_filename, render_order_xlsx
from msbot.moysklad import MoySkladClient, MoySkladError
from msbot.orders import OrderService


async def main(numbers: list, dry: bool, save_dir: str = "") -> int:
    settings = load_settings()
    if not settings.notify_chat_id and not dry and not save_dir:
        print("Не задан NOTIFY_CHAT_ID — некуда отправлять")
        return 1

    client = MoySkladClient(settings.moysklad_token, settings.moysklad_base_url)
    service = OrderService(client, settings.store_name, clock=settings.clock())
    bot = None if (dry or save_dir) else Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    failed = 0
    try:
        for number in numbers:
            try:
                card = await service.card_by_number(number)
            except MoySkladError as exc:
                print(f"  ✗ {number}: {exc}")
                failed += 1
                continue

            if card is None:
                print(f"  ✗ {number}: заказ не найден")
                failed += 1
                continue

            cells = sum(1 for position in card.positions if position.slots)
            note = (f"{card.state}, позиций {len(card.positions)}"
                    f" (с ячейками {cells})")

            if save_dir:
                target = pathlib.Path(save_dir)
                target.mkdir(parents=True, exist_ok=True)
                path = target / order_filename(card)
                path.write_bytes(render_order_xlsx(card, settings.compact_slots))
                print(f"  ✓ {number}: {note} → {path}")
            elif dry:
                print(f"  ✓ {number}: {note}, формат {settings.order_format}")
            else:
                await send_card(bot, settings.notify_chat_id, card, settings)
                print(f"  ✓ {number}: {note} → отправлено")
    finally:
        await client.aclose()
        if bot:
            await bot.session.close()
    return failed


if __name__ == "__main__":
    argv = sys.argv[1:]
    save_dir = ""
    if "--save" in argv:
        index = argv.index("--save")
        save_dir = argv[index + 1] if index + 1 < len(argv) else "."
        del argv[index:index + 2]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(args, "--dry" in argv, save_dir)))
