#!/usr/bin/env bash
# Деплой/обновление бота на сервере. Запускать с этой машины (Mac):
#
#   ./deploy/deploy.sh user@host
#
# Что делает:
#   1. Проверяет SSH-доступ.
#   2. На сервере создаёт системного пользователя moysklad-bot (без входа) и
#      каталог /opt/moysklad-bot, если их ещё нет.
#   3. Синхронизирует код (rsync), НЕ трогая .env, state.json, batch_state.json
#      на сервере — их обновление приходится ровно на первый запуск и на
#      руки кладовщика, автоматика их не переписывает.
#   4. Ставит/обновляет venv и зависимости.
#   5. Кладёт systemd-юнит, делает `daemon-reload`, `enable`, `restart`.
#   6. Показывает последние строки журнала — сразу видно, поднялся бот или нет.
#
# Повторный запуск того же скрипта — штатный способ выкатить обновление кода.

set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="/opt/moysklad-bot"
SERVICE_USER="moysklad-bot"
SERVICE_NAME="moysklad-bot"

if [ -z "$TARGET" ]; then
    echo "Использование: $0 user@host" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "== Проверка SSH-доступа к $TARGET =="
ssh -o ConnectTimeout=10 "$TARGET" 'echo "  ✓ подключение есть, whoami: $(whoami)"'

echo
echo "== Подготовка пользователя и каталога на сервере =="
ssh "$TARGET" bash -s <<EOSSH
set -e
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    sudo useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  создан системный пользователь $SERVICE_USER"
else
    echo "  пользователь $SERVICE_USER уже есть"
fi
sudo mkdir -p "$REMOTE_DIR"
# Владелец временно — тот, кто заливает файлы по SSH (обычно не root и не
# сервисный пользователь): rsync ниже пишет без sudo. Финальный chown на
# $SERVICE_USER — отдельным шагом после синхронизации.
sudo chown "\$(whoami):\$(whoami)" "$REMOTE_DIR"
EOSSH

echo
echo "== Синхронизация кода (rsync) =="
# .env / state.json / batch_state.json на сервере не трогаем: их источник
# истины — сам сервер после первого запуска, а не эта машина.
rsync -az --delete \
    --exclude '.venv/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'state.json' \
    --exclude 'batch_state.json' \
    --exclude '.DS_Store' \
    "$PROJECT_DIR"/ "$TARGET:$REMOTE_DIR"/
ssh "$TARGET" "sudo chown -R $SERVICE_USER:$SERVICE_USER $REMOTE_DIR"
echo "  код скопирован"

echo
echo "== .env на сервере =="
if ssh "$TARGET" "sudo test -f $REMOTE_DIR/.env"; then
    echo "  .env уже есть на сервере — не перезаписываю"
else
    echo "  .env отсутствует — копирую .env.example как заготовку."
    echo "  ⚠️  Обязательно заполните TELEGRAM_TOKEN и MOYSKLAD_TOKEN на сервере"
    echo "      перед первым запуском: ssh $TARGET, потом nano $REMOTE_DIR/.env"
    scp "$PROJECT_DIR/.env.example" "$TARGET:/tmp/moysklad-bot.env.example"
    ssh "$TARGET" "sudo mv /tmp/moysklad-bot.env.example $REMOTE_DIR/.env && \
        sudo chown $SERVICE_USER:$SERVICE_USER $REMOTE_DIR/.env && \
        sudo chmod 600 $REMOTE_DIR/.env"
fi

echo
echo "== Виртуальное окружение и зависимости =="
ssh "$TARGET" bash -s <<EOSSH
set -e
cd "$REMOTE_DIR"
if [ ! -d .venv ]; then
    sudo -u "$SERVICE_USER" python3 -m venv .venv
fi
sudo -u "$SERVICE_USER" .venv/bin/pip install -q --upgrade pip
sudo -u "$SERVICE_USER" .venv/bin/pip install -q -r requirements.txt
echo "  зависимости установлены"
EOSSH

echo
echo "== systemd-юнит =="
scp "$SCRIPT_DIR/moysklad-bot.service" "$TARGET:/tmp/${SERVICE_NAME}.service"
ssh "$TARGET" bash -s <<EOSSH
set -e
sudo mv "/tmp/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
EOSSH

echo
echo "== Перезапуск сервиса =="
ssh "$TARGET" "sudo systemctl restart ${SERVICE_NAME}"
sleep 3
ssh "$TARGET" "sudo systemctl status ${SERVICE_NAME} --no-pager -l | head -15"

echo
echo "== Последние строки журнала =="
ssh "$TARGET" "sudo journalctl -u ${SERVICE_NAME} -n 20 --no-pager"

echo
echo "Готово. Полный журнал: ssh $TARGET 'sudo journalctl -u ${SERVICE_NAME} -f'"
