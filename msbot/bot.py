"""Telegram-бот складовщика: карточка заказа покупателя из МойСклад."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings, load_settings
from .delivery import send_card
from .moysklad import MoySkladClient, MoySkladError
from .batcher import PickingBatcher
from .notifier import NewOrderNotifier
from .orders import OrderService

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Бот показывает заказ покупателя из МойСклад вместе с адресами хранения.\n\n"
    "<b>Как пользоваться</b>\n"
    "• Отправьте номер заказа сообщением — например <code>00123</code>\n"
    "• /order <i>номер</i> — то же самое явной командой\n"
    "• /find <i>текст</i> — поиск заказа по номеру или контрагенту\n"
    "• /refresh — сбросить кэш ячеек склада\n"
    "• /chatid — id чата (нужен для настройки оповещений)\n\n"
    "В карточке: номер, статус, дата, канал продаж, позиции с количеством "
    "и ячейками адресного хранения (в скобках — остаток в ячейке)."
)


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из белого списка (если он задан)."""

    def __init__(self, allowed_ids: set, allowed_chat_id: int = 0) -> None:
        super().__init__()
        self._allowed = allowed_ids
        self._allowed_chat_id = allowed_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Доступ открыт всем, только если не задан ни чат склада, ни белый список
        if not self._allowed and not self._allowed_chat_id:
            return await handler(event, data)

        # /chatid нужен, чтобы узнать id чата ещё до настройки доступа
        if isinstance(event, Message) and (event.text or "").startswith("/chatid"):
            return await handler(event, data)

        chat = data.get("event_chat")
        if self._allowed_chat_id and chat and chat.id == self._allowed_chat_id:
            return await handler(event, data)

        user = data.get("event_from_user")
        if user and user.id in self._allowed:
            return await handler(event, data)

        log.warning("Отказано в доступе: user_id=%s", getattr(user, "id", None))
        if isinstance(event, Message):
            await event.answer("Нет доступа. Обратитесь к администратору.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def cmd_chatid(message: Message) -> None:
    """Подсказывает id чата — его нужно вписать в NOTIFY_CHAT_ID."""
    await message.answer(
        f"id этого чата: <code>{message.chat.id}</code>\n"
        f"Ваш id: <code>{message.from_user.id}</code>"
    )


async def cmd_refresh(message: Message, service: OrderService) -> None:
    service.refresh_caches()
    try:
        info = await service.check_connection()
    except MoySkladError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(f"Кэш обновлён.\nСклад: <b>{info}</b>")


async def cmd_order(
    message: Message,
    command: CommandObject,
    service: OrderService,
    settings: Settings,
) -> None:
    number = (command.args or "").strip()
    if not number:
        await message.answer("Укажите номер заказа: <code>/order 00123</code>")
        return
    await _send_order_by_number(message, service, number, settings)


async def cmd_find(message: Message, command: CommandObject, service: OrderService) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Что искать? Например: <code>/find Ромашка</code>")
        return

    try:
        found = await service.search(query)
    except MoySkladError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    if not found:
        await message.answer("Ничего не найдено.")
        return

    builder = InlineKeyboardBuilder()
    for order_id, number, subtitle in found:
        builder.row(
            InlineKeyboardButton(
                text=f"{number} — {subtitle}"[:64],
                callback_data=f"order:{order_id}",
            )
        )
    await message.answer("Найденные заказы:", reply_markup=builder.as_markup())


async def plain_number(message: Message, service: OrderService, settings: Settings) -> None:
    text = (message.text or "").strip()
    # В общем чате склада бот не должен отвечать на всю переписку:
    # реагируем только на сообщение, целиком состоящее из номера заказа.
    if message.chat.type != "private" and not _looks_like_order_number(text):
        return
    await _send_order_by_number(message, service, text, settings)


def _looks_like_order_number(text: str) -> bool:
    return text.isdigit() and 3 <= len(text) <= 32


async def open_order(callback: CallbackQuery, service: OrderService, settings: Settings) -> None:
    order_id = callback.data.split(":", 1)[1]
    await callback.answer()
    try:
        card = await service.card_by_id(order_id)
    except MoySkladError as exc:
        await callback.message.answer(f"⚠️ {exc}")
        return
    await send_card(callback.bot, callback.message.chat.id, card, settings)


async def _send_order_by_number(
    message: Message,
    service: OrderService,
    number: str,
    settings: Settings,
) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        card = await service.card_by_number(number)
    except MoySkladError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception:  # noqa: BLE001 — не роняем бота на одном заказе
        log.exception("Ошибка при сборке заказа %s", number)
        await message.answer("Не удалось получить заказ. Попробуйте ещё раз.")
        return

    if card is None:
        await message.answer(
            f"Заказ <b>{number}</b> не найден.\n"
            "Номер должен совпадать точно — или используйте /find."
        )
        return

    await send_card(message.bot, message.chat.id, card, settings)


async def on_added_to_chat(event: ChatMemberUpdated) -> None:
    """Бота добавили в чат — сразу подсказываем id для NOTIFY_CHAT_ID."""
    chat = event.chat
    log.info("Бот добавлен в чат %s (id=%s)", chat.title or chat.type, chat.id)
    await event.bot.send_message(
        chat.id,
        "Готов к работе.\n\n"
        f"id этого чата: <code>{chat.id}</code>\n"
        "Впишите его в <code>NOTIFY_CHAT_ID</code> и перезапустите бота — "
        "тогда сюда будут приходить новые заказы.",
    )


def build_router() -> Router:
    """Свежий роутер на каждый вызов: один Router нельзя привязать к двум Dispatcher."""
    router = Router()
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_chatid, Command("chatid"))
    router.message.register(cmd_refresh, Command("refresh"))
    router.message.register(cmd_order, Command("order"))
    router.message.register(cmd_find, Command("find"))
    router.message.register(plain_number, F.text & ~F.text.startswith("/"))
    router.callback_query.register(open_order, F.data.startswith("order:"))
    router.my_chat_member.register(
        on_added_to_chat,
        ChatMemberUpdatedFilter(member_status_changed=(IS_NOT_MEMBER >> IS_MEMBER)),
    )
    return router


def build_dispatcher(settings: Settings, service: OrderService) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["service"] = service
    dispatcher["settings"] = settings

    access = AccessMiddleware(settings.allowed_user_ids, settings.notify_chat_id)
    dispatcher.message.middleware(access)
    dispatcher.callback_query.middleware(access)

    dispatcher.include_router(build_router())
    return dispatcher


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()

    client = MoySkladClient(settings.moysklad_token, settings.moysklad_base_url)
    service = OrderService(
        client,
        settings.store_name,
        slots_ttl=settings.slots_ttl,
        store_ttl=settings.store_ttl,
        clock=settings.clock(),
    )

    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher(settings, service)

    try:
        log.info("Проверка доступа к МойСклад…")
        log.info("Адресный склад: %s", await service.check_connection())
        log.info("Часовые пояса: %s", settings.clock().describe())
    except MoySkladError as exc:
        log.error("МойСклад недоступен: %s", exc)

    tasks = []
    if settings.notify_chat_id:
        # Порядок важен: notifier кладёт заказы в очередь batcher-а —
        # ссылка нужна ему уже на старте.
        batcher = PickingBatcher(bot, service, settings)
        notifier = NewOrderNotifier(bot, client, service, settings, batcher)
        tasks.append(asyncio.create_task(notifier.start()))
        tasks.append(asyncio.create_task(batcher.start()))
        log.info(
            "Оповещения о новых заказах: чат %s, опрос раз в %s c%s",
            settings.notify_chat_id,
            settings.poll_interval,
            f", статусы: {', '.join(settings.notify_states)}" if settings.notify_states else "",
        )
    else:
        log.info("Оповещения выключены: не задан NOTIFY_CHAT_ID")

    try:
        await dispatcher.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.aclose()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
