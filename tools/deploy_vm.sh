#!/bin/bash
# Развёртывание кода на VM в Yandex Cloud.
#
# Что делает:
#   1. отправляет код в /home/yc-user/parser (кроме venv, базы, логов и секретов);
#   2. перезапускает systemd-службу;
#   3. проверяет, что дашборд отвечает.
#
# Что НЕ трогает: config.txt на VM (там свои настройки, включая OZON_BROWSER),
# базу data/sellers.sqlite3 (её нельзя затирать — в ней заметки и статусы),
# venv, output и .env (у облачного дашборда свой пароль — локальный .env
# затирать нельзя; только явный --with-env).
#
# Запуск:  bash tools/deploy_vm.sh
# Только код, без перезапуска:  bash tools/deploy_vm.sh --no-restart
# Ещё и .env с Мака:             bash tools/deploy_vm.sh --with-env

set -euo pipefail

VM="${OZON_VM:-yc-user@51.250.84.76}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/ozon_crm_cloud}"
REMOTE_DIR="/home/yc-user/parser"
# Ключ хоста зафиксирован в deploy/vm_known_hosts (аудит: без проверки
# ключа MITM на этом канале получает .env). Обновить: ssh-keyscan -H <host>
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$(dirname "$0")/vm_known_hosts" -o ConnectTimeout=15)
RESTART=1
WITH_ENV=0
for arg in "$@"; do
  [ "$arg" = "--no-restart" ] && RESTART=0
  [ "$arg" = "--with-env" ] && WITH_ENV=1
done

cd "$(dirname "$0")/.."
echo "Развёртывание на $VM:$REMOTE_DIR"

# --- 1. Код --------------------------------------------------------------- #
echo
echo "[1/4] Отправляю код…"
rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude 'venv/' \
  --exclude '.venv/' \
  --exclude 'tmpvenv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude 'config.txt' \
  --exclude 'data/' \
  --exclude 'output/' \
  --exclude 'logs/' \
  --exclude 'outreach_data/' \
  --exclude 'backups/' \
  --exclude '*.tar.gz' \
  ./ "$VM:$REMOTE_DIR/"
echo "  код отправлен"

# --- 2. Секреты ----------------------------------------------------------- #
echo
if [ "$WITH_ENV" = "1" ]; then
  echo "[2/4] Настраиваю .env…"
  if [ -f .env ]; then
    # Пароль передаём файлом, а не аргументом: аргументы видны в списке процессов
    scp -q "${SSH_OPTS[@]}" .env "$VM:$REMOTE_DIR/.env"
    ssh "${SSH_OPTS[@]}" "$VM" "chmod 600 $REMOTE_DIR/.env"
    echo "  .env на месте, права 600"
  else
    echo "  .env локально не найден — на VM останется прежний"
  fi
else
  echo "[2/4] .env не трогаю (облачный пароль свой); для перезаписи: --with-env"
fi

# --- 3. Зависимости ------------------------------------------------------- #
echo
echo "[3/4] Проверяю зависимости…"
ssh "${SSH_OPTS[@]}" "$VM" "
  cd $REMOTE_DIR
  # curl_cffi обязателен: без него WB-воркер не стартует (WB закрывает
  # обычный requests по TLS-отпечатку). Если чего-то нет — ставим всё.
  # Аудит: проверка 3 из 9 пакетов пропускала отсутствие остальных —
  # служба перезапускалась в падение. pip install идемпотентен и быстр.
  .venv/bin/pip install -q -r requirements.txt \
    && echo '  зависимости requirements.txt: установлены' \
    || echo '  ВНИМАНИЕ: pip install завершился с ошибкой — проверьте службу'
"

# --- 4. Перезапуск -------------------------------------------------------- #
echo
if [ "$RESTART" = "1" ]; then
  echo "[4/4] Перезапускаю службу…"
  ssh "${SSH_OPTS[@]}" "$VM" "
    sudo systemctl restart parser-dashboard.service
    sleep 4
    systemctl is-active parser-dashboard.service | sed 's/^/  состояние: /'
  "
else
  echo "[4/4] Перезапуск пропущен (--no-restart)"
fi

echo
echo "Проверка дашборда:"
# Порт 8000 закрыт извне (v1.8.3) — проверяем публичный HTTPS:
# 401 — норма, дашборд закрыт паролем из .env
curl -s -o /dev/null -w "  https://ozon.sh-coding.ru/  ->  %{http_code}\n" \
  --max-time 10 "https://ozon.sh-coding.ru/" || echo "  не отвечает"
echo
echo "Готово. Логи: ssh $VM 'journalctl -u parser-dashboard -f'"
