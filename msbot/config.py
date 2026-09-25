"""Конфигурация бота: читается из переменных окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Set

from dotenv import load_dotenv

from .timezones import DEFAULT_DISPLAY_TZ, DEFAULT_MOYSKLAD_TZ, Clock

DEFAULT_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
DEFAULT_STORE_NAME = "Склад адрес. хранение"


@dataclass
class Settings:
    telegram_token: str
    moysklad_token: str
    moysklad_base_url: str = DEFAULT_BASE_URL
    store_name: str = DEFAULT_STORE_NAME
    allowed_user_ids: Set[int] = field(default_factory=set)
    compact_slots: bool = True
    table_width: int = 58
    order_format: str = "xlsx"

    # Часовые пояса: аккаунт МойСклад и склад живут в разных зонах
    moysklad_tz: str = DEFAULT_MOYSKLAD_TZ
    display_tz: str = DEFAULT_DISPLAY_TZ

    def clock(self) -> Clock:
        return Clock(self.moysklad_tz, self.display_tz)

    # Оповещения о новых заказах в чат склада
    notify_chat_id: int = 0
    poll_interval: int = 60
    notify_states: List[str] = field(default_factory=list)
    state_file: str = "state.json"
    notify_since: str = "today"

    # Маршрутизация: Kaspi без экспресса -> сводный лист, с экспрессом и
    # прочие каналы (ОПТ) -> отдельным сообщением
    kaspi_channel: str = "Kaspi магазин"
    express_attribute: str = "Экспресс доставка"
    batch_size: int = 10
    batch_max_wait: int = 60          # минут
    batch_interval: int = 60          # секунд между проверками пула
    batch_state_file: str = "batch_state.json"

    # ОПТ: заказ заводят пустым и наполняют вручную, поэтому ждём статус,
    # который подтверждает готовность к отгрузке
    opt_channel: str = "ОПТ"
    opt_states: List[str] = field(default_factory=lambda: ["Моя Доставка"])

    # TTL кэшей, секунды
    slots_ttl: int = 600
    store_ttl: int = 3600
    stock_ttl: int = 30


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_list(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_ids(raw: str) -> Set[int]:
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            ids.add(int(chunk))
    return ids


def load_settings() -> Settings:
    load_dotenv()

    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    moysklad_token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if not telegram_token:
        raise RuntimeError("Не задан TELEGRAM_TOKEN")
    if not moysklad_token:
        raise RuntimeError("Не задан MOYSKLAD_TOKEN")

    return Settings(
        telegram_token=telegram_token,
        moysklad_token=moysklad_token,
        moysklad_base_url=os.getenv("MOYSKLAD_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        store_name=os.getenv("MOYSKLAD_STORE_NAME", DEFAULT_STORE_NAME).strip(),
        allowed_user_ids=_parse_ids(os.getenv("ALLOWED_USER_IDS", "")),
        compact_slots=_parse_bool(os.getenv("COMPACT_SLOT_NAMES", "true")),
        table_width=int(os.getenv("TABLE_WIDTH", "58") or 0),
        order_format=os.getenv("ORDER_FORMAT", "xlsx").strip().lower() or "xlsx",
        moysklad_tz=os.getenv("MOYSKLAD_TZ", DEFAULT_MOYSKLAD_TZ).strip() or DEFAULT_MOYSKLAD_TZ,
        display_tz=os.getenv("DISPLAY_TZ", DEFAULT_DISPLAY_TZ).strip() or DEFAULT_DISPLAY_TZ,
        notify_chat_id=int(os.getenv("NOTIFY_CHAT_ID", "0") or 0),
        poll_interval=int(os.getenv("POLL_INTERVAL", "60") or 60),
        notify_states=_parse_list(os.getenv("NOTIFY_STATES", "")),
        state_file=os.getenv("STATE_FILE", "state.json"),
        notify_since=os.getenv("NOTIFY_SINCE", "today").strip() or "today",
        kaspi_channel=os.getenv("KASPI_CHANNEL", "Kaspi магазин").strip() or "Kaspi магазин",
        express_attribute=os.getenv("EXPRESS_ATTRIBUTE", "Экспресс доставка").strip()
        or "Экспресс доставка",
        batch_size=int(os.getenv("BATCH_SIZE", "10") or 10),
        batch_max_wait=int(os.getenv("BATCH_MAX_WAIT_MINUTES", "60") or 60),
        batch_interval=int(os.getenv("BATCH_INTERVAL", "60") or 60),
        batch_state_file=os.getenv("BATCH_STATE_FILE", "batch_state.json"),
        opt_channel=os.getenv("OPT_CHANNEL", "ОПТ").strip() or "ОПТ",
        opt_states=_parse_list(os.getenv("OPT_STATES", "")) or ["Моя Доставка"],
    )
