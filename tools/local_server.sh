#!/bin/bash
# Локальный сервер дашборда — для правок кода без деплоя на VM.
#
#   tools/local_server.sh start|stop|restart|status
#
# Сервер живёт отдельным процессом (nohup), лог — logs/local_server.log.
# Авторестарта нет: после правок кода делай restart, чтобы они вступили в силу.
#
# Чтобы локальный дашборд не открывал Chrome с Ozon раз в полчаса
# (автопроверка чатов), в config.txt должно стоять OZON_INBOX_AUTO=0.

set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${OZON_LOCAL_PORT:-8000}"
PID_FILE="data/local_server.pid"
LOG_FILE="logs/local_server.log"

is_running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
  if is_running; then
    echo "Уже запущен (pid $(cat "$PID_FILE")): http://127.0.0.1:$PORT/"
    return 0
  fi
  mkdir -p logs data
  # Логи старше 30 дней не нужны: без чистки каталог рос вечно (аудит)
  find logs -type f -name "*.log" -mtime +30 -delete 2>/dev/null || true
  # BROWSER=none — чтобы каждый start/restart не открывал новую вкладку
  # браузера: адрес и так напечатан ниже
  nohup env BROWSER=none .venv/bin/python dashboard/server.py \
    --host 127.0.0.1 --port "$PORT" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  if is_running; then
    echo "Запущен (pid $(cat "$PID_FILE")): http://127.0.0.1:$PORT/"
    echo "Лог: $LOG_FILE"
  else
    echo "Не запустился — хвост $LOG_FILE:" >&2
    tail -5 "$LOG_FILE" >&2
    exit 1
  fi
}

stop() {
  if is_running; then
    kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE"
    echo "Остановлен"
  else
    rm -f "$PID_FILE"
    echo "Не был запущен"
  fi
}

status() {
  if is_running; then
    echo "Запущен (pid $(cat "$PID_FILE")): http://127.0.0.1:$PORT/"
  else
    echo "Не запущен"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "Использование: tools/local_server.sh start|stop|restart|status"; exit 1 ;;
esac
