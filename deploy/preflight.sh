#!/usr/bin/env bash
# Проверка сервера ДО деплоя: доступность нужных API и версия Python.
# Запускать прямо на сервере (или через ssh host 'bash -s' < preflight.sh).
#
# Ничего не устанавливает и не меняет — только диагностика.

set -u
fail=0

echo "== ОС и Python =="
if command -v lsb_release >/dev/null 2>&1; then
    lsb_release -ds
elif [ -f /etc/os-release ]; then
    . /etc/os-release; echo "$PRETTY_NAME"
fi
if command -v python3 >/dev/null 2>&1; then
    python3 --version
else
    echo "✗ python3 не найден"; fail=1
fi

echo
echo "== Доступность Telegram API =="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://api.telegram.org 2>/dev/null || echo "000")
if [ "$code" = "302" ] || [ "$code" = "200" ]; then
    echo "✓ api.telegram.org отвечает ($code)"
else
    echo "✗ api.telegram.org недоступен (код $code) — бот не сможет получать обновления."
    echo "  Если сервер в РФ, вероятна блокировка: понадобится прокси/VPN для исходящих запросов."
    fail=1
fi

echo
echo "== Доступность МойСклад API =="
# Без Accept-Encoding: gzip МойСклад отвечает 415, а не 401 — это не блокировка,
# просто такое требование самого API. Шлём заголовок, как это делает сам бот.
code=$(curl -s --compressed -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Accept-Encoding: gzip" https://api.moysklad.ru/api/remap/1.2/entity/store 2>/dev/null || echo "000")
if [ "$code" = "401" ]; then
    echo "✓ api.moysklad.ru отвечает ожидаемым 401 (сервис жив, дело за токеном)"
elif [ "$code" != "000" ]; then
    echo "~ api.moysklad.ru отвечает кодом $code (не 401, но соединение есть — проверьте вручную)"
else
    echo "✗ api.moysklad.ru недоступен"
    fail=1
fi

echo
echo "== Место на диске и права =="
df -h / | tail -1
if [ "$(id -u)" = "0" ] || sudo -n true 2>/dev/null; then
    echo "✓ есть sudo/root — можно ставить системного пользователя и systemd-юнит"
else
    echo "~ sudo без пароля не подтверждён — деплой-скрипт может спросить пароль"
fi

echo
if [ "$fail" = "0" ]; then
    echo "Готово к деплою."
else
    echo "Есть блокирующие пункты (см. ✗ выше) — деплой скорее всего не заработает как есть."
fi
exit "$fail"
