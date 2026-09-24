#!/usr/bin/env bash
#
# Ubuntu 24.04 компьютер бүр дээр agent-ийг суулгах скрипт.
# Ашиглах:  sudo ./install.sh <TOKEN>
#   <TOKEN>  — controller-тэй ижил нууц түлхүүр (заавал өгнө)
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "root эрхээр ажиллуулна уу:  sudo ./install.sh <TOKEN>" >&2
    exit 1
fi

TOKEN="${1:-}"
if [[ -z "$TOKEN" ]]; then
    echo "Алдаа: TOKEN өгөөгүй байна.  Жишээ: sudo ./install.sh mySecret123" >&2
    exit 1
fi

PORT="${2:-8765}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Шаардлагатай багцуудыг суулгаж байна (screenshot, notify хэрэгслүүд)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
# python3 нь Ubuntu 24.04-т бэлэн. Screenshot/мэдэгдлийн хэрэгслүүд:
apt-get install -y --no-install-recommends \
    python3 gnome-screenshot scrot imagemagick zenity libnotify-bin \
    python3-pil sudo || true

echo "==> Файлуудыг байрлуулж байна..."
install -d /opt/monitoring-agent
install -m 0755 "$SRC_DIR/agent.py" /opt/monitoring-agent/agent.py

install -d /etc/monitoring-agent
cat > /etc/monitoring-agent/config.json <<EOF
{
  "port": ${PORT},
  "token": "${TOKEN}",
  "screenshot_max_width": 1280,
  "screenshot_quality": 55
}
EOF
chmod 600 /etc/monitoring-agent/config.json

echo "==> systemd service тохируулж байна..."
install -m 0644 "$SRC_DIR/monitoring-agent.service" /etc/systemd/system/monitoring-agent.service
systemctl daemon-reload
systemctl enable --now monitoring-agent.service

echo "==> Галт хана (ufw) идэвхтэй бол портыг нээж байна..."
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" || true
fi

sleep 1
systemctl --no-pager --lines=5 status monitoring-agent.service || true
echo
echo "✔ Дууслаа. Энэ машины IP хаягууд:"
hostname -I
echo "Controller дээр эдгээр IP-г scan хийж олно.  Порт: ${PORT}"
