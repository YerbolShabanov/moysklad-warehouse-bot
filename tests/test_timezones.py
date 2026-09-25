"""Часовые пояса: аккаунт МойСклад (МСК) и склад (Алматы) расходятся на 2 часа."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msbot.config import Settings
from msbot.timezones import Clock

ALMATY = ZoneInfo("Asia/Almaty")


def main():
    clock = Clock("Europe/Moscow", "Asia/Almaty")

    # Разбор: МойСклад отдаёт МСК, показываем в Алматы (+2 часа)
    moment = clock.parse("2026-09-10 09:38:03.000")
    assert moment.strftime("%d.%m.%Y %H:%M") == "10.09.2026 11:38", moment
    assert clock.parse("2026-09-10 09:38:03").hour == 11
    assert clock.parse(None) is None
    assert clock.parse("не дата") is None

    # Дата документа переезжает через полночь
    assert clock.parse("2026-09-09 23:30:00.000").strftime("%d.%m %H:%M") == "10.09 01:30"

    # Фильтр: полночь в Алматы — это 22:00 предыдущего дня по Москве
    midnight = datetime(2026, 9, 10, 0, 0, tzinfo=ALMATY)
    assert clock.to_moysklad(midnight) == "2026-09-09 22:00:00", clock.to_moysklad(midnight)
    assert clock.to_moysklad(clock.today_start(midnight)) == "2026-09-09 22:00:00"

    # Наивное время трактуем как время склада
    assert clock.to_moysklad(datetime(2026, 9, 10, 12, 0)) == "2026-09-10 10:00:00"

    # today_start обрезает время, сохраняя пояс склада
    noon = datetime(2026, 9, 10, 14, 30, tzinfo=ALMATY)
    start = clock.today_start(noon)
    assert (start.hour, start.minute, start.tzinfo) == (0, 0, ALMATY), start

    # Неизвестный пояс не роняет бота, а откатывается к значению по умолчанию
    fallback = Clock("Нет/Такого", "Тоже/Нет")
    assert fallback.moysklad.key == "Europe/Moscow"
    assert fallback.display.key == "Asia/Almaty"

    # Значения по умолчанию в настройках — те, что нужны складу
    settings = Settings(telegram_token="t", moysklad_token="m")
    assert settings.moysklad_tz == "Europe/Moscow"
    assert settings.display_tz == "Asia/Almaty"
    assert settings.clock().display.key == "Asia/Almaty"

    # Если оба пояса совпадают, преобразований нет
    same = Clock("Asia/Almaty", "Asia/Almaty")
    assert same.to_moysklad(midnight) == "2026-09-10 00:00:00"

    print("OK: даты показываются по Алматы, фильтры уходят по Москве")


if __name__ == "__main__":
    main()
