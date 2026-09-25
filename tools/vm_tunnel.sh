#!/bin/bash
# Публикует локальный дашборд по адресу VM: http://130.193.51.137:8000
#
# Почему так: Ozon блокирует и датацентровые IP, и серверное окружение браузера
# (софтверный WebGL/Xvfb). Поэтому сам парсинг идёт на этом Mac с домашним IP,
# а VM даёт публичный адрес. Mac должен быть включён, туннель — запущен.
VM=ubuntu@130.193.51.137
KEY=~/.ssh/travel_bot_yandex
cd "$(dirname "$0")/.."

if ! curl -s -o /dev/null --max-time 3 http://127.0.0.1:8000/api/health; then
  echo "Запускаю локальный дашборд..."
  nohup .venv/bin/python dashboard/server.py --host 127.0.0.1 --port 8000 > /tmp/dashboard.log 2>&1 &
  sleep 6
fi

echo "Публичный адрес: http://130.193.51.137:8000  (остановить: Ctrl+C)"
while true; do
  ssh -N -R 0.0.0.0:8000:127.0.0.1:8000 -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
      -i "$KEY" "$VM"
  echo "Туннель отвалился, переподключаюсь через 5 секунд..."
  sleep 5
done
