#!/bin/bash
# Подключает релей Telegram (Cloudflare Worker) к CRM на VM.
#
# Использование:
#   bash tools/tg_relay_setup.sh https://tg-relay.имя.workers.dev
#
# Что делает: прописывает TELEGRAM_API_BASE в .env на VM, перезапускает
# дашборд и отправляет тестовое уведомление. Если в Telegram пришло — готово.

set -euo pipefail
cd "$(dirname "$0")/.."

URL="${1:?Укажи URL воркера: bash tools/tg_relay_setup.sh https://tg-relay.xxx.workers.dev}"
case "$URL" in
  https://*) ;;
  *) echo "URL должен начинаться с https://"; exit 1 ;;
esac

VM="${OZON_VM:-yc-user@51.250.84.76}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/ozon_crm_cloud}"
KNOWN_HOSTS="$(cd "$(dirname "$0")/.." && pwd)/deploy/vm_known_hosts"
IFACE="${OZON_VM_IFACE:-en0}"
SSH_OPTS=(-i "$KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes
          -o BindInterface="$IFACE" -o BatchMode=yes -o ConnectTimeout=15)

echo "1) Прописываю TELEGRAM_API_BASE в .env на VM…"
ssh "${SSH_OPTS[@]}" "$VM" "cd /home/yc-user/parser && \
  sed -i '/^TELEGRAM_API_BASE=/d' .env && \
  echo 'TELEGRAM_API_BASE=$URL' >> .env && \
  grep -c 'TELEGRAM_API_BASE' .env"

echo "2) Перезапускаю дашборд…"
ssh "${SSH_OPTS[@]}" "$VM" "sudo -n systemctl restart parser-dashboard && sleep 3 && systemctl is-active parser-dashboard"

echo "3) Тестовая отправка через релей…"
ssh "${SSH_OPTS[@]}" "$VM" "cd /home/yc-user/parser && .venv/bin/python -c \"
from src.notify import telegram as tg
print('база релея:', tg._api_base())
ok = tg.send('✅ Релей Telegram подключён: уведомления CRM теперь приходят сюда. Команды бота: /help')
print('отправлено:', ok)\

\""

echo "Готово. Если выше 'отправлено: True' и сообщение пришло в чат — всё работает."
