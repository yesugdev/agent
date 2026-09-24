#!/usr/bin/env bash
#
# Ubuntu машин дээрээс monitoring-agent-ийг бүрэн устгах скрипт.
# Ашиглах:  sudo bash uninstall.sh
#
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "root эрхээр ажиллуулна уу:  sudo bash uninstall.sh" >&2
    exit 1
fi

PORT_GUESS=8765
if [[ -f /etc/monitoring-agent/config.json ]]; then
    P=$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' /etc/monitoring-agent/config.json | grep -oE '[0-9]+' || true)
    [[ -n "${P:-}" ]] && PORT_GUESS="$P"
fi

echo "==> Service-ийг зогсоож, идэвхгүй болгож байна..."
systemctl stop monitoring-agent.service 2>/dev/null || true
systemctl disable monitoring-agent.service 2>/dev/null || true

echo "==> systemd unit файлыг устгаж байна..."
rm -f /etc/systemd/system/monitoring-agent.service
systemctl daemon-reload
systemctl reset-failed monitoring-agent.service 2>/dev/null || true

echo "==> Програм болон тохиргооны файлуудыг устгаж байна..."
rm -rf /opt/monitoring-agent
rm -rf /etc/monitoring-agent

echo "==> Галт ханын дүрмийг устгаж байна (ufw)..."
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw delete allow "${PORT_GUESS}/tcp" 2>/dev/null || true
fi

echo "==> Үлдэгдэл процесс байвал зогсоож байна..."
pkill -f "/opt/monitoring-agent/agent.py" 2>/dev/null || true

echo
echo "✔ monitoring-agent бүрэн устгагдлаа."
echo "  Шалгах:  systemctl status monitoring-agent   (олдохгүй байх ёстой)"
