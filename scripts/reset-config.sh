#!/bin/sh
# scripts/reset-config.sh — сброс voxnode к заводским настройкам
#
# Удаляет:
#   - маркер wifi-configured → при следующей загрузке стартует captive portal
#   - все WiFi-профили NetworkManager (кроме системных: lo, ethernet)
#
# После запуска: перезагрузка → AP voxnode-setup-XXXX → форма на 192.168.4.1
#
# Использование (на малине):
#   sudo voxnode-reset
# или
#   sudo /opt/voxnode/scripts/reset-config.sh

set -e

WIFI_MARKER="/etc/voxnode/wifi-configured"

log()  { printf '[reset] %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужен root. Запустите: sudo $0" >&2
    exit 1
fi

log "удаляю маркер wifi-configured..."
rm -f "$WIFI_MARKER"

log "удаляю WiFi-профили NetworkManager..."
# Перечисляем все wifi-подключения и удаляем каждое
nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep ':802-11-wireless:' | \
    while IFS=: read -r name _type; do
        log "  удаляю: $name"
        nmcli connection delete "$name" 2>/dev/null || true
    done

log "удаляю AP-профиль voxnode-setup (если остался)..."
nmcli connection delete voxnode-setup 2>/dev/null || true

log ""
log "✓ сброс завершён."
log "При следующей загрузке малина поднимет AP 'voxnode-setup-XXXX'."
log "Подключитесь к ней телефоном и откройте http://192.168.4.1"
log ""
log "Перезагрузка через 3 секунды... (Ctrl+C для отмены)"
sleep 3
reboot
