#!/bin/bash
# SSH-туннель до дашборда на VM (аудит: старый скрипт указывал на мёртвую VM
# ubuntu@130.193.51.137 и спавнил дашборд-сироту без pid-файла с логом в /tmp).
#
#   tools/vm_tunnel.sh          # туннель http://127.0.0.1:8001 -> VM:8000
#   tools/vm_tunnel.sh stop     # остановить туннель

set -euo pipefail
cd "$(dirname "$0")/.."

VM="${OZON_VM:-yc-user@51.250.84.76}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/ozon_crm_cloud}"
PID_FILE="data/vm_tunnel.pid"
LOG_FILE="logs/vm_tunnel.log"
LOCAL_PORT=8001

mkdir -p logs data

if [ "${1:-start}" = "stop" ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")"
    echo "Туннель остановлен"
  else
    rm -f "$PID_FILE"
    echo "Туннель не был запущен"
  fi
  exit 0
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Туннель уже работает: http://127.0.0.1:$LOCAL_PORT/"
  exit 0
fi

nohup ssh -i "$KEY" -o StrictHostKeyChecking=no -N \
  -L "$LOCAL_PORT:127.0.0.1:8000" "$VM" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 2
echo "Туннель поднят: http://127.0.0.1:$LOCAL_PORT/ (лог: $LOG_FILE)"
