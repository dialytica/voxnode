"""Captive portal для первичной настройки WiFi.

Сценарий:
  1. Малина только установлена / сброшена — нет сохранённого WiFi-профиля.
  2. voxnode-portal.service стартует (условие: нет маркера wifi-configured).
  3. Поднимает AP 'voxnode-setup-XXXX' через NetworkManager.
  4. Пользователь подключается телефоном, открывает 192.168.4.1.
  5. Заполняет форму: SSID + пароль + server_url + device_id.
  6. Малина создаёт NM-профиль клиента, ставит маркер, перезагружается.
  7. После ребута — клиентский режим, запись стартует.

DNS hijack: все DNS-запросы от клиентов AP перенаправляются на 192.168.4.1,
HTTP-запросы ловит Flask. ОС телефона (Android/iOS) детектит captive portal
и автоматически открывает форму.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, render_template_string, request

from voxnode.config import load_config
from voxnode.version import get_version

log = logging.getLogger("voxnode.portal")

# Маркер того, что WiFi уже настроен (создаётся после первой настройки).
WIFI_CONFIGURED_MARKER = Path("/etc/voxnode/wifi-configured")

# Имя AP-подключения в NetworkManager
AP_CONNECTION_NAME = "voxnode-setup"

# Подсеть для AP-режима
AP_GATEWAY = "192.168.4.1"
AP_SSID_PREFIX = "voxnode-setup-"

PORTAL_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voxnode — настройка</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0f172a; color: #e2e8f0;
    margin: 0; padding: 20px; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: #1e293b; border-radius: 16px;
    padding: 28px 24px; max-width: 420px; width: 100%;
    box-shadow: 0 10px 40px rgba(0,0,0,0.5);
  }
  .logo {
    text-align: center; margin-bottom: 24px;
    font-weight: 700; font-size: 26px; color: #38bdf8;
    letter-spacing: -0.5px;
  }
  .logo small { display:block; font-size:12px; color:#64748b; font-weight:400; margin-top:4px; }
  label { display:block; margin: 14px 0 6px; font-size: 13px; color:#94a3b8; }
  input, select {
    width:100%; padding: 12px 14px; border-radius: 10px;
    border: 1px solid #334155; background:#0f172a; color:#e2e8f0;
    font-size: 16px; outline: none;
  }
  input:focus { border-color: #38bdf8; }
  .row { display: flex; gap: 10px; }
  .row > div { flex: 1; }
  button {
    width: 100%; margin-top: 24px; padding: 14px;
    background: linear-gradient(135deg, #38bdf8, #0ea5e9);
    color: white; border: 0; border-radius: 10px;
    font-size: 16px; font-weight: 600; cursor: pointer;
  }
  button:active { transform: scale(0.98); }
  .msg { padding: 12px; border-radius: 10px; margin-top: 16px; font-size: 14px; }
  .msg.ok  { background: #064e3b; color: #6ee7b7; }
  .msg.err { background: #7f1d1d; color: #fca5a5; }
  .hint { font-size:12px; color:#64748b; margin-top:6px; }
  .wifi-list { margin-top: 6px; max-height: 140px; overflow-y: auto; }
  .wifi-item { padding: 10px 12px; margin: 4px 0; background:#0f172a; border-radius:8px;
               cursor: pointer; font-size: 14px; border: 1px solid #334155; }
  .wifi-item:active { background:#1e293b; }
  .ver { text-align:center; color:#475569; font-size:11px; margin-top:16px; }
</style>
</head>
<body>
<form class="card" method="post" action="/save">
  <div class="logo">🎙️ voxnode<small>настройка точки записи</small></div>

  {% if error %}
    <div class="msg err">{{ error }}</div>
  {% endif %}

  <label for="ssid">WiFi сеть (SSID)</label>
  <input id="ssid" name="ssid" list="wifi-networks" required value="{{ last_ssid or '' }}"
         placeholder="Выберите или введите имя сети">
  <datalist id="wifi-networks">
    {% for net in wifi_networks %}
      <option value="{{ net }}">
    {% endfor %}
  </datalist>
  {% if wifi_networks %}
  <div class="hint">Найдено сетей: {{ wifi_networks|length }}. Нажмите на поле, чтобы выбрать.</div>
  {% endif %}

  <label for="wifi_password">Пароль WiFi</label>
  <input id="wifi_password" name="wifi_password" type="password" required
         placeholder="Пароль от WiFi">

  <label for="device_id">ID устройства</label>
  <input id="device_id" name="device_id" required value="{{ device_id }}"
         placeholder="например, shop-moscow-01">
  <div class="hint">Уникальное имя этой точки в проекте dialytica</div>

  <label for="server_url">Сервер dialytica</label>
  <input id="server_url" name="server_url" required value="{{ server_url }}"
         placeholder="https://api.dialytica.ru">

  <label for="device_secret">Секрет устройства</label>
  <input id="device_secret" name="device_secret" value="{{ device_secret }}"
         placeholder="выдаётся при регистрации точки">

  <button type="submit">Сохранить и подключиться →</button>
  <div class="ver">voxnode {{ version }}</div>
</form>
</body>
</html>
"""

SUCCESS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voxnode — настройка</title>
<style>
  body { font-family: system-ui, sans-serif; background:#0f172a; color:#e2e8f0;
         margin:0; padding:20px; min-height:100vh; display:flex;
         align-items:center; justify-content:center; text-align:center; }
  .card { background:#1e293b; padding:40px 28px; border-radius:16px; max-width:380px; }
  h1 { color:#6ee7b7; margin:0 0 12px; font-size:42px; }
  p { color:#94a3b8; line-height:1.5; margin:8px 0; }
  .ssid { color:#38bdf8; font-weight:600; }
</style>
</head>
<body>
<div class="card">
  <h1>✓</h1>
  <p><b>Настройки сохранены.</b></p>
  <p>Малина подключается к <span class="ssid">{{ ssid }}</span>.</p>
  <p>Через ~30 секунд запись начнётся автоматически.<br>
     Можно закрыть это окно.</p>
</div>
</body>
</html>
"""


def get_device_suffix() -> str:
    """Случайный 4-символьный суффикс для уникальности AP (из MAC wlan0)."""
    try:
        mac = Path("/sys/class/net/wlan0/address").read_text().strip()
        return mac.replace(":", "")[-4:].upper()
    except OSError:
        return "0000"


def start_ap() -> bool:
    """Поднять точку доступа через NetworkManager (shared)."""
    suffix = get_device_suffix()
    ssid = f"{AP_SSID_PREFIX}{suffix}"

    log.info("поднимаю AP: %s", ssid)

    # Удаляем старый AP-профиль, если есть
    subprocess.run(
        ["nmcli", "connection", "delete", AP_CONNECTION_NAME],
        capture_output=True, text=True,
    )

    # Создаём shared Wi-Fi подключение
    cmd = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "ifname", "wlan0",
        "con-name", AP_CONNECTION_NAME,
        "autoconnect", "no",
        "ssid", ssid,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "ipv4.method", "shared",
        "ipv4.addresses", f"{AP_GATEWAY}/24",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("nmcli add провалился: %s", result.stderr)
        return False

    # Поднимаем
    result = subprocess.run(
        ["nmcli", "connection", "up", AP_CONNECTION_NAME],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        log.error("nmcli up провалился: %s", result.stderr)
        return False

    log.info("AP поднята, gateway=%s", AP_GATEWAY)
    return True


def scan_wifi() -> list[str]:
    """Сканировать доступные WiFi-сети (для подсказки в форме)."""
    try:
        subprocess.run(["nmcli", "device", "wifi", "rescan"], capture_output=True, timeout=10)
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        ssids = []
        for line in result.stdout.splitlines():
            ssid = line.strip()
            if ssid and ssid not in ssids:
                ssids.append(ssid)
        return ssids[:15]  # топ-15
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def stop_ap() -> None:
    """Остановить AP перед переключением в клиентский режим."""
    log.info("останавливаю AP")
    subprocess.run(
        ["nmcli", "connection", "down", AP_CONNECTION_NAME],
        capture_output=True, text=True,
    )


def add_client_wifi(ssid: str, password: str) -> bool:
    """Создать клиентский NM-профиль и активировать."""
    log.info("создаю клиентский профиль для SSID=%s", ssid)
    cmd = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "ifname", "wlan0",
        "con-name", ssid,
        "sssid", ssid,  # будет исправлено ниже (опечатка nmcli)
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "10",
    ]
    # nmcli использует 'ssid' без префикса для подключения wifi
    cmd[cmd.index("sssid")] = "ssid"

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("nmcli add client провалился: %s", result.stderr)
        return False
    return True


def run_portal() -> int:
    """Главная точка входа — поднимает AP и web-форму."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = load_config()
    log.info("portal starting; version=%s", get_version())

    # 1. Поднимаем AP
    if not start_ap():
        log.error("не удалось поднять AP, выхожу")
        return 1

    # 2. Flask-приложение
    app = Flask(__name__)
    wifi_cache: list[str] = []

    def refresh_wifi_async() -> None:
        """Фоновое сканирование сетей — не блокирует старт."""
        time.sleep(2)
        networks = scan_wifi()
        wifi_cache.extend(networks)
        log.info("найдено %d WiFi-сетей", len(networks))

    threading.Thread(target=refresh_wifi_async, daemon=True).start()

    @app.route("/", methods=["GET"])
    def index():
        return render_template_string(
            PORTAL_HTML,
            wifi_networks=wifi_cache,
            device_id=cfg.uploader.device_id,
            server_url=cfg.uploader.server_url or "https://api.dialytica.ru",
            device_secret=cfg.uploader.device_secret,
            last_ssid="",
            error=None,
            version=get_version(),
        )

    @app.route("/save", methods=["POST"])
    def save():
        ssid = request.form.get("ssid", "").strip()
        wifi_password = request.form.get("wifi_password", "")
        device_id = request.form.get("device_id", "").strip()
        server_url = request.form.get("server_url", "").strip()
        device_secret = request.form.get("device_secret", "").strip()

        # Валидация
        if not ssid or not wifi_password or not device_id or not server_url:
            return render_template_string(
                PORTAL_HTML,
                wifi_networks=wifi_cache,
                device_id=device_id or cfg.uploader.device_id,
                server_url=server_url or cfg.uploader.server_url,
                device_secret=device_secret,
                last_ssid=ssid,
                error="Заполните все обязательные поля",
                version=get_version(),
            ), 400

        # Пишем конфиг
        from voxnode.config import find_config_path
        cfg_path = find_config_path()
        if cfg_path:
            _update_config(cfg_path, device_id, server_url, device_secret)

        # Создаём клиентский WiFi-профиль
        add_client_wifi(ssid, wifi_password)

        # Маркер: WiFi настроен
        WIFI_CONFIGURED_MARKER.touch()
        try:
            WIFI_CONFIGURED_MARKER.chown(1000, 1000)  # contai
        except (PermissionError, OSError):
            pass

        # Переключение делаем в отдельном потоке, чтобы ответ успел уйти клиенту
        def switch_and_reboot():
            time.sleep(3)
            stop_ap()
            # Активируем клиентский профиль
            subprocess.run(["nmcli", "connection", "up", ssid], capture_output=True, timeout=20)
            time.sleep(5)
            subprocess.run(["sudo", "reboot"], capture_output=True)

        threading.Thread(target=switch_and_reboot, daemon=True).start()

        return render_template_string(SUCCESS_HTML, ssid=ssid)

    # captive portal detection: Android/iOS/Windows запрашивают эти URL
    @app.route("/generate_204")
    @app.route("/hotspot-detect.html")
    @app.route("/connecttest.txt")
    def captive_probe():
        # Возвращаем редирект на главную — ОС откроет форму автоматически
        from flask import redirect
        return redirect("/", code=302)

    # Запускаем на 80 порту, на всех интерфейсах AP
    app.run(host="0.0.0.0", port=80, debug=False, use_reloader=False)
    return 0


def _update_config(cfg_path: Path, device_id: str, server_url: str, device_secret: str) -> None:
    """Обновить /etc/voxnode/config.yaml — заменить device_id, server_url, device_secret."""
    import yaml
    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("uploader", {})
    raw["uploader"]["device_id"] = device_id
    raw["uploader"]["server_url"] = server_url
    if device_secret:
        raw["uploader"]["device_secret"] = device_secret
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, default_flow_style=False)
    log.info("конфиг обновлён: device_id=%s server=%s", device_id, server_url)


if __name__ == "__main__":
    raise SystemExit(run_portal())
