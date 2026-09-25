"""Проверка проводки хендлеров aiogram без реальных сетевых вызовов."""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SendChatAction, SendDocument, SendMessage
from aiogram.types import Chat, Message, Update, User

from msbot.bot import build_dispatcher
from msbot.config import Settings
from msbot.orders import OrderService
from test_smoke import FakeClient

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class RecordingSession:
    """Подменяет транспорт бота: копит вызовы вместо запросов в Telegram."""

    def __init__(self):
        self.sent = []

    async def __call__(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return _fake_message(method.text)
        return True

    async def close(self):
        pass


GROUP_ID = -1001234567890


def _fake_message(text="", user_id=42, chat_id=None, chat_type="private"):
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Кладовщик"),
        text=text,
    )


def _update(text, user_id=42, chat_id=None, chat_type="private"):
    return Update(
        update_id=1,
        message=_fake_message(text, user_id, chat_id, chat_type),
    )


async def feed(dispatcher, bot, session, text, user_id=42, chat_id=None, chat_type="private"):
    session.sent.clear()
    await dispatcher.feed_update(bot, _update(text, user_id, chat_id, chat_type))
    return [m.text for m in session.sent if isinstance(m, SendMessage)]


async def main():
    session = RecordingSession()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session = session

    client = FakeClient()
    service = OrderService(client, "Склад адрес. хранение")
    settings = Settings(
        telegram_token=TOKEN,
        moysklad_token="x",
        allowed_user_ids={42},
        notify_chat_id=GROUP_ID,
        order_format="text",
    )
    dispatcher = build_dispatcher(settings, service)

    texts = await feed(dispatcher, bot, session, "/start")
    assert texts and "Как пользоваться" in texts[0], texts

    texts = await feed(dispatcher, bot, session, "00123")
    assert any("Заказ покупателя 00123" in t for t in texts), texts
    assert any("Р10-М9-Э2" in t for t in texts), texts
    assert any("Наименование" in t for t in texts), texts
    assert any("4690624257353" in t for t in texts), texts
    assert any(isinstance(m, SendChatAction) for m in session.sent)

    texts = await feed(dispatcher, bot, session, "/order 00123")
    assert any("Канал продаж: Ozon" in t for t in texts), texts

    texts = await feed(dispatcher, bot, session, "/order")
    assert "Укажите номер заказа" in texts[0], texts

    texts = await feed(dispatcher, bot, session, "77777")
    assert "не найден" in texts[0], texts

    texts = await feed(dispatcher, bot, session, "/refresh")
    assert "Кэш обновлён" in texts[0], texts

    # Пользователь вне белого списка не получает данных заказа
    texts = await feed(dispatcher, bot, session, "00123", user_id=999)
    assert texts == ["Нет доступа. Обратитесь к администратору."], texts

    # /chatid работает даже у постороннего — иначе не настроить оповещения
    texts = await feed(dispatcher, bot, session, "/chatid", user_id=999)
    assert "id этого чата" in texts[0] and "999" in texts[0], texts

    # В чате склада бот молчит на обычную переписку
    texts = await feed(
        dispatcher, bot, session, "Ребята, кто на смене?",
        user_id=777, chat_id=GROUP_ID, chat_type="supergroup",
    )
    assert texts == [], texts

    # ...но отвечает на голый номер заказа, и доступ даёт весь чат целиком
    texts = await feed(
        dispatcher, bot, session, "00123",
        user_id=777, chat_id=GROUP_ID, chat_type="supergroup",
    )
    assert any("Заказ покупателя 00123" in t for t in texts), texts

    # В личке — прежнее поведение: любой текст трактуется как номер
    texts = await feed(dispatcher, bot, session, "абв")
    assert "не найден" in texts[0], texts

    # Когда доступ держится составом группы, личка посторонним закрыта
    open_settings = Settings(
        telegram_token=TOKEN, moysklad_token="x",
        allowed_user_ids=set(), notify_chat_id=GROUP_ID, order_format="text",
    )
    guarded = build_dispatcher(open_settings, service)
    texts = await feed(guarded, bot, session, "00123", user_id=555)
    assert texts == ["Нет доступа. Обратитесь к администратору."], texts
    texts = await feed(
        guarded, bot, session, "00123",
        user_id=555, chat_id=GROUP_ID, chat_type="supergroup",
    )
    assert any("Заказ покупателя 00123" in t for t in texts), texts

    # В режиме xlsx заказ уходит файлом, а не сообщением
    xlsx_settings = Settings(
        telegram_token=TOKEN, moysklad_token="x",
        allowed_user_ids={42}, notify_chat_id=GROUP_ID, order_format="xlsx",
    )
    files = build_dispatcher(xlsx_settings, service)
    session.sent.clear()
    await files.feed_update(bot, _update("00123"))
    documents = [m for m in session.sent if isinstance(m, SendDocument)]
    assert len(documents) == 1, session.sent
    assert documents[0].document.filename == "Заказ-00123.xlsx"
    assert "Заказ покупателя 00123" in documents[0].caption
    assert documents[0].document.data[:2] == b"PK"  # xlsx — это zip

    print("OK: хендлеры отвечают корректно, чат склада обслуживается")


asyncio.run(main())
