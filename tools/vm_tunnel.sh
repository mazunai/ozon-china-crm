#!/bin/bash
# SSH-туннели до VM: дашборд, noVNC и отладочный порт Chrome.
#
#   tools/vm_tunnel.sh          # поднять: 8001->дашборд, 6080->noVNC, 9222->Chrome
#   tools/vm_tunnel.sh stop     # остановить
#
# Про VPN. Если на Mac включён VPN (hidemyname и подобные), он заворачивает весь
# трафик в utun* и соединение до VM отваливается на этапе обмена баннером SSH:
# TCP-рукопожатие проходит, дальше тишина. Поэтому туннель принудительно идёт
# через физический интерфейс (OZON_VM_IFACE, по умолчанию определяется сам).

set -euo pipefail
cd "$(dirname "$0")/.."

VM="${OZON_VM:-yc-user@51.250.84.76}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/ozon_crm_cloud}"
PID_FILE="data/vm_tunnel.pid"
LOG_FILE="logs/vm_tunnel.log"

# Порты: локальный:удалённый
FORWARDS=(
  "8001:127.0.0.1:8000"   # дашборд
  "6080:127.0.0.1:6080"   # noVNC -> http://localhost:6080/vnc.html
  "9222:127.0.0.1:9222"   # Chrome DevTools (для run_outreach.py)
)

mkdir -p logs data

if [ "${1:-start}" = "stop" ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
    echo "Туннель остановлен"
  else
    rm -f "$PID_FILE"
    echo "Туннель не был запущен"
  fi
  exit 0
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Туннель уже работает: http://127.0.0.1:6080/vnc.html"
  exit 0
fi

# Физический интерфейс: перебираем активные, отбрасывая VPN-туннели и loopback.
if [ -n "${OZON_VM_IFACE:-}" ]; then
  IFACE="$OZON_VM_IFACE"
else
  IFACE=$(ifconfig -l | tr ' ' '\n' | grep -E '^(en|eth)' | while read -r i; do
    ifconfig "$i" 2>/dev/null | grep -q 'status: active' && echo "$i" && break
  done)
  IFACE="${IFACE:-en0}"
fi

OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -N)
# BindInterface есть не везде — если ssh его не знает, туннель всё равно поднимется.
if ssh -i "$KEY" -o BindInterface="$IFACE" -o ConnectTimeout=8 -o BatchMode=yes "$VM" true 2>/dev/null; then
  OPTS+=(-o BindInterface="$IFACE")
  echo "Трафик пойдёт через $IFACE (в обход VPN)"
else
  echo "ВНИМАНИЕ: не удалось привязаться к $IFACE, иду штатным маршрутом"
fi

LINKS=()
for fwd in "${FORWARDS[@]}"; do LINKS+=(-L "$fwd"); done

nohup ssh "${OPTS[@]}" "${LINKS[@]}" "$VM" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 3

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Туннель поднят:"
  echo "  Вход в Ozon : http://localhost:6080/vnc.html"
  echo "  Дашборд     : http://localhost:8001/"
  echo "  Chrome :9222: localhost:9222 (для run_outreach.py)"
else
  echo "Туннель не поднялся, смотрите $LOG_FILE"
  exit 1
fi
