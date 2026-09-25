"""Часовые пояса: аккаунт МойСклад и склад живут в разных зонах.

МойСклад отдаёт и принимает даты в часовом поясе аккаунта, **не указывая
смещение** — фильтр вида `2026-09-10 00:00:00+0500` API отклоняет (400), а
сам пояс через API не сообщается. Аккаунт заведён по Москве (UTC+3), склад
работает в Алматы (UTC+5), поэтому:

* всё, что показываем людям, переводим в пояс склада (`DISPLAY_TZ`);
* всё, что уходит в фильтры API, переводим обратно в пояс аккаунта
  (`MOYSKLAD_TZ`) и отдаём без смещения — как того требует API.

Без этого «заказы с начала текущего дня» смещались бы на два часа: полночь
в Алматы — это 22:00 предыдущего дня по Москве.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MS_DATETIME = "%Y-%m-%d %H:%M:%S"
DEFAULT_MOYSKLAD_TZ = "Europe/Moscow"
DEFAULT_DISPLAY_TZ = "Asia/Almaty"


class Clock:
    """Переводы между поясом аккаунта МойСклад и поясом склада."""

    def __init__(
        self,
        moysklad_tz: str = DEFAULT_MOYSKLAD_TZ,
        display_tz: str = DEFAULT_DISPLAY_TZ,
    ) -> None:
        self.moysklad = _zone(moysklad_tz, DEFAULT_MOYSKLAD_TZ)
        self.display = _zone(display_tz, DEFAULT_DISPLAY_TZ)

    # ------------------------------------------------------------- разбор

    def parse(self, raw: Optional[str]) -> Optional[datetime]:
        """Строка МойСклад -> момент времени в поясе склада."""
        if not raw:
            return None
        for fmt in (MS_DATETIME + ".%f", MS_DATETIME):
            try:
                naive = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            return naive.replace(tzinfo=self.moysklad).astimezone(self.display)
        return None

    # ------------------------------------------------------------ фильтры

    def to_moysklad(self, moment: datetime) -> str:
        """Момент времени -> строка для фильтра API (пояс аккаунта, без смещения)."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.display)
        return moment.astimezone(self.moysklad).strftime(MS_DATETIME)

    def now_display(self) -> datetime:
        return datetime.now(self.display)

    def today_start(self, now: Optional[datetime] = None) -> datetime:
        """Начало текущего дня по часам склада."""
        moment = now or self.now_display()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.display)
        else:
            moment = moment.astimezone(self.display)
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)

    def describe(self) -> str:
        now = self.now_display()
        return (
            f"склад {self.display.key} ({now:%H:%M}), "
            f"аккаунт МойСклад {self.moysklad.key} "
            f"({now.astimezone(self.moysklad):%H:%M})"
        )


def _zone(name: str, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(fallback)
