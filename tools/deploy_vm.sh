#!/bin/bash
# Развёртывание кода на VM в Yandex Cloud.
#
# Что делает:
#   1. отправляет код в /opt/parser_ozon (кроме venv, базы, логов и секретов);
#   2. кладёт .env с паролем дашборда;
#   3. перезапускает systemd-службу;
#   4. проверяет, что дашборд отвечает.
#
# Что НЕ трогает: config.txt на VM (там свои настройки, включая OZON_BROWSER),
# базу data/sellers.sqlite3 (её нельзя затирать — в ней заметки и статусы),
# venv и output.
#
# Запуск:  bash tools/deploy_vm.sh
# Только код, без перезапуска:  bash tools/deploy_vm.sh --no-restart

set -euo pipefail

VM="${OZON_VM:-ubuntu@130.193.51.137}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/travel_bot_yandex}"
REMOTE_DIR="/opt/parser_ozon"
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15)
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

cd "$(dirname "$0")/.."
echo "Развёртывание на $VM:$REMOTE_DIR"

# --- 1. Код --------------------------------------------------------------- #
echo
echo "[1/4] Отправляю код…"
rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude 'venv/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude 'config.txt' \
  --exclude 'data/' \
  --exclude 'output/' \
  --exclude 'logs/' \
  --exclude 'outreach_data/' \
  --exclude '*.tar.gz' \
  ./ "$VM:$REMOTE_DIR/"
echo "  код отправлен"

# --- 2. Секреты ----------------------------------------------------------- #
echo
echo "[2/4] Настраиваю .env…"
if [ -f .env ]; then
  # Пароль передаём файлом, а не аргументом: аргументы видны в списке процессов
  scp -q "${SSH_OPTS[@]}" .env "$VM:$REMOTE_DIR/.env"
  ssh "${SSH_OPTS[@]}" "$VM" "chmod 600 $REMOTE_DIR/.env"
  echo "  .env на месте, права 600"
else
  echo "  .env локально не найден — на VM останется прежний"
fi

# --- 3. Зависимости ------------------------------------------------------- #
echo
echo "[3/4] Проверяю зависимости…"
ssh "${SSH_OPTS[@]}" "$VM" "
  cd $REMOTE_DIR
  venv/bin/python -c 'import selenium, camoufox, openpyxl' 2>/dev/null \
    && echo '  selenium + camoufox + openpyxl: есть' \
    || { echo '  ставлю недостающее…'; venv/bin/pip install -q -r requirements.txt; }
"

# --- 4. Перезапуск -------------------------------------------------------- #
echo
if [ "$RESTART" = "1" ]; then
  echo "[4/4] Перезапускаю службу…"
  ssh "${SSH_OPTS[@]}" "$VM" "
    sudo systemctl restart ozon-dashboard.service
    sleep 4
    systemctl is-active ozon-dashboard.service | sed 's/^/  состояние: /'
  "
else
  echo "[4/4] Перезапуск пропущен (--no-restart)"
fi

echo
echo "Проверка дашборда:"
curl -s -o /dev/null -w "  http://$VM:8000/  ->  %{http_code}\n" \
  --max-time 10 "http://${VM#*@}:8000/" || echo "  не отвечает"
echo "  (401 — это норма: дашборд закрыт паролем из .env)"
echo
echo "Готово. Логи: ssh $VM 'journalctl -u ozon-dashboard -f'"
