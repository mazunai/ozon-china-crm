#!/bin/bash
# Удалённый доступ к экрану VM — чтобы войти в Ozon руками, когда сессия вылетела.
#
# Зачем: Chrome парсера живёт на VM в виртуальном дисплее Xvfb :99, окна не
# видно. На VM поднят x11vnc (служба x11vnc.service), но он привязан к
# 127.0.0.1 — наружу порт не выставлен. Поэтому смотрим через SSH-туннель,
# пароль VNC лежит в ~/.x11vnc.pass на VM (расшифровка: openssl enc -d
# -des-ecb -K e84ad660c4721ae0 -nopad, ключ VNC фиксированный).
#
#   tools/vnc_tunnel.sh          # туннель 127.0.0.1:5900 -> VM:5900
#   tools/vnc_tunnel.sh stop     # остановить туннель
#   tools/vnc_tunnel.sh status   # состояние
#
# После запуска смотреть: Finder -> Cmd+K -> vnc://127.0.0.1:5900
# (или `open vnc://127.0.0.1:5900` — откроется «Общий экран»).
#
# Туннель висит, пока Mac не уснёт. Упал — просто запусти скрипт снова.

set -euo pipefail
cd "$(dirname "$0")/.."

VM="${OZON_VM:-yc-user@51.250.84.76}"
KEY="${OZON_VM_KEY:-$HOME/.ssh/ozon_crm_cloud}"
KNOWN_HOSTS="$(dirname "$0")/../deploy/vm_known_hosts"
PID_FILE="data/vnc_tunnel.pid"
LOG_FILE="logs/vnc_tunnel.log"
LOCAL_PORT="${OZON_VNC_PORT:-5900}"
REMOTE_PORT=5900

# Если на Mac поднят VPN, он заворачивает весь трафик в utun* и ssh до VM
# отваливается на обмене баннером: TCP-рукопожатие проходит, дальше тишина.
# Поэтому туннель принудительно идёт через физический интерфейс.
pick_iface() {
  if [ -n "${OZON_VM_IFACE:-}" ]; then echo "$OZON_VM_IFACE"; return; fi
  for i in $(ifconfig -l); do
    case "$i" in
      utun*|lo*|gif*|stf*|bridge*|awdl*|llw*|ap*|anpi*) continue ;;
    esac
    if ifconfig "$i" 2>/dev/null | grep -q 'status: active'; then echo "$i"; return; fi
  done
  echo en0
}

mkdir -p logs data

is_running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-start}" in
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")"
      rm -f "$PID_FILE"
      echo "Туннель VNC остановлен"
    else
      rm -f "$PID_FILE"
      echo "Туннель VNC не был запущен"
    fi
    ;;

  status)
    if is_running; then
      echo "Работает (pid $(cat "$PID_FILE")): vnc://127.0.0.1:$LOCAL_PORT"
    else
      echo "Не запущен"
    fi
    ;;

  start)
    if is_running; then
      echo "Уже работает: vnc://127.0.0.1:$LOCAL_PORT"
      exit 0
    fi

    # Порт занят — почти всегда прежний туннель, переживший потерю pid-файла
    if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "Порт $LOCAL_PORT уже занят — освободи его или задай OZON_VNC_PORT" >&2
      lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >&2
      exit 1
    fi

    IFACE="$(pick_iface)"
    SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=yes
              -o UserKnownHostsFile="$KNOWN_HOSTS" -o ConnectTimeout=15
              -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
              -o Compression=yes)   # VNC-кадры хорошо жмутся: -C заметно ускоряет отрисовку
    # BindInterface есть не везде: если ssh его не знает или интерфейс не тот,
    # туннель всё равно поднимается штатным маршрутом.
    if ssh -i "$KEY" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS" \
           -o BindInterface="$IFACE" -o ConnectTimeout=8 -o BatchMode=yes "$VM" true 2>/dev/null; then
      SSH_OPTS+=(-o BindInterface="$IFACE")
      echo "Трафик пойдёт через $IFACE (в обход VPN)"
    fi

    nohup ssh "${SSH_OPTS[@]}" \
      -N -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$VM" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    # Ждём не «две секунды вообще», а именно открытия порта: с exit-on-failure
    # ssh умирает сразу, так что проверка честная
    for _ in $(seq 1 15); do
      if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Туннель поднят: vnc://127.0.0.1:$LOCAL_PORT  (лог: $LOG_FILE)"
        echo "Дальше: open vnc://127.0.0.1:$LOCAL_PORT"
        exit 0
      fi
      sleep 1
    done

    echo "Туннель не поднялся — хвост $LOG_FILE:" >&2
    tail -5 "$LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
    ;;

  *)
    echo "Использование: tools/vnc_tunnel.sh start|stop|status" >&2
    exit 1
    ;;
esac
