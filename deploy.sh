#!/usr/bin/env bash
# Развёртывание Системы ППР на Ubuntu Server.
#
# Запуск (из папки проекта на сервере):
#   sudo ./deploy.sh
#
# Что делает:
#   1) ставит python3, venv, pip
#   2) создаёт системного пользователя ppr
#   3) копирует приложение в /opt/ppr-system
#   4) создаёт виртуальное окружение и ставит зависимости
#   5) регистрирует systemd-сервис ppr (автозапуск при загрузке)
#   6) настраивает ежедневное резервное копирование БД (в 02:00)

set -euo pipefail

APP_DIR="/opt/ppr-system"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_USER="ppr"
SERVICE_FILE="/etc/systemd/system/ppr.service"
BACKUP_DIR="/var/backups/ppr"
PORT="${PPR_PORT:-8000}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Ошибка: нужны права root. Запустите: sudo ./deploy.sh"
  exit 1
fi

if [ ! -d "$SCRIPT_DIR/app" ] || [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
  echo "Ошибка: рядом со скриптом нет папки app/ или requirements.txt."
  exit 1
fi

echo "==> Установка системных пакетов..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

echo "==> Создание пользователя $SERVICE_USER..."
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Копирование приложения в $APP_DIR..."
mkdir -p "$APP_DIR/app"
cp -r "$SCRIPT_DIR/app/." "$APP_DIR/app/"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SCRIPT_DIR/backup_ppr.sh" "$APP_DIR/backup_ppr.sh"

echo "==> Виртуальное окружение и зависимости..."
if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Права..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chmod +x "$APP_DIR/backup_ppr.sh"

echo "==> systemd-сервис..."
sed "s|@APP_DIR@|$APP_DIR|g; s|@SERVICE_USER@|$SERVICE_USER|g; s|@PORT@|$PORT|g" \
  "$SCRIPT_DIR/ppr.service" > "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable ppr
systemctl restart ppr

echo "==> Резервное копирование (ежедневно в 02:00)..."
mkdir -p "$BACKUP_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$BACKUP_DIR"
cat > /etc/cron.d/ppr_backup <<EOF
0 2 * * * $SERVICE_USER $APP_DIR/backup_ppr.sh
EOF

echo
echo "Развёртывание завершено."
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Доступ:"
echo "  На сервере:  http://localhost:$PORT"
if [ -n "$IP" ]; then
  echo "  По сети:     http://$IP:$PORT"
fi
echo
echo "Проверка статуса:  systemctl status ppr"
echo "Логи:              journalctl -u ppr -f"
echo "Бэкапы БД:         $BACKUP_DIR (хранятся 30 копий)"
echo
echo "Если включён брандмауэр ufw, откройте порт:"
echo "  sudo ufw allow $PORT/tcp"
echo
echo "ВАЖНО: смените пароли по умолчанию (engineer/engineer123, boss/boss123) после первого входа."