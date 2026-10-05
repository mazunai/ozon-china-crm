#!/bin/bash
# Запуск отправщика для КАЖДОГО аккаунта из outreach_data/accounts.json.
#
# Каждый аккаунт — отдельный процесс со своим профилем Chrome, портом
# отладки, дневным лимитом и стоп-краном; очередь в базе общая.
#
# Использование:
#   bash tools/start_senders.sh --dry-run   # предпросмотр (без отправки)
#   bash tools/start_senders.sh             # рабочий режим по sender.json
#
# Логи: logs/sender_<id>.log. Остановить все: touch outreach_data/STOP,
# один аккаунт: touch outreach_data/STOP_<id>. Статус реестра:
#   python3 run_outreach.py accounts

set -u
cd "$(dirname "$0")/.."

mkdir -p logs

IDS=$(python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, '.')
from src.outreach.accounts import load_accounts
print(' '.join(a.id for a in load_accounts(Path('outreach_data'))))
")

if [ -z "$IDS" ]; then
    echo "Реестр аккаунтов пуст — добавьте: python3 run_outreach.py accounts --add mgr2"
    exit 1
fi

for id in $IDS; do
    if [ -f "data/sender_${id}.pid" ] && kill -0 "$(cat "data/sender_${id}.pid")" 2>/dev/null; then
        echo "Аккаунт $id уже работает (pid $(cat "data/sender_${id}.pid")) — пропускаю"
        continue
    fi
    nohup python3 run_outreach.py send --account "$id" "$@" \
        > "logs/sender_${id}.log" 2>&1 &
    echo $! > "data/sender_${id}.pid"
    echo "Аккаунт $id запущен (pid $!), лог: logs/sender_${id}.log"
done

echo
echo "Остановить все:    touch outreach_data/STOP"
echo "Остановить один:   touch outreach_data/STOP_<id>"
echo "Список аккаунтов:  python3 run_outreach.py accounts"
