"""Store-and-forward отправка аудио на dialytica-сервер.

Uploader работает как постоянный демон:
1. Сканирует буфер (RAM + spill) на наличие готовых сегментов
2. Сортирует по времени (старые отправляются раньше — важный порядок для транскрипции)
3. Для каждого файла: POST на сервер с HMAC-подписью и заголовком X-Voxnode-Version
4. Успех (200) → файл удаляется
5. Провал (4xx/5xx/timeout) → экспоненциальный backoff, файл остаётся

Частичные файлы (.part — пишущийся recorder'ом) игнорируются.
Файл, который не удалось отправить за max_retries, переезжает в spill_dir
(SD) — чтобы не забивать RAM-буфер.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

from voxnode.config import Config, load_config
from voxnode.version import get_version

log = logging.getLogger("voxnode.uploader")

# Расширения, которые uploader обрабатывает
AUDIO_EXTS = {".opus", ".ogg", ".wav", ".mp3", ".flac", ".m4a"}
PARTIAL_SUFFIX = ".part"

# Интервал между проходами сканера, когда буфер пуст
IDLE_INTERVAL_SEC = 5
# Интервал между попытками отправки при ошибке (до backoff)
ERROR_INTERVAL_SEC = 10


def find_ready_segments(cfg: Config) -> list[Path]:
    """Найти все готовые к отправке сегменты (отсортированы по имени = по времени).

    Частичные (.part) файлы пропускаются — они ещё пишутся recorder'ом.
    """
    ram = Path(cfg.buffer.ram_dir)
    spill = Path(cfg.buffer.spill_dir)
    found: list[Path] = []

    for directory in (ram, spill):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.name.endswith(PARTIAL_SUFFIX):
                continue
            if entry.suffix.lower() not in AUDIO_EXTS:
                continue
            # Готовность: размер > 0 и не менялся последние 2 секунды (не пишется)
            try:
                size = entry.stat().st_size
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if size == 0:
                continue
            if time.time() - mtime < 2:
                continue
            found.append(entry)

    # Сортировка по имени = по timestamp (устройство_YYYYmmddTHHMMSSZ.opus)
    found.sort(key=lambda p: p.name)
    return found


def parse_segment_metadata(filename: str) -> dict[str, str]:
    """Извлечь device_id и timestamp из имени файла.

    Формат: {device_id}_{YYYYmmddTHHMMSSZ}.{ext}
    device_id может содержать '-': shop-moscow-01_20260717T153200Z.opus
    """
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    if "_" not in name:
        return {}
    # Разделяем по последнему '_' — timestamp всегда последний
    device_id, _, ts = name.rpartition("_")
    return {"device_id": device_id, "timestamp": ts}


def compute_signature(secret: str, file_path: Path) -> str:
    """HMAC-SHA256 от SHA256 файла. Защита от подделки записей на сервере."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    file_digest = sha256.hexdigest()
    return hmac.new(
        secret.encode("utf-8"),
        file_digest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def upload_one(cfg: Config, file_path: Path) -> tuple[bool, str | None]:
    """Отправить один файл. Возвращает (success, error_msg).

    Success=True: сервер подтвердил, файл можно удалять.
    Success=False: провал, файл оставить (error_msg — для лога).
    """
    u = cfg.uploader
    version = get_version()

    if not u.server_url:
        return False, "server_url не задан в конфиге"

    url = urljoin(u.server_url.rstrip("/") + "/", u.upload_endpoint.lstrip("/"))
    meta = parse_segment_metadata(file_path.name)

    # timestamp уже ISO-8601 basic (UTC); расширяем до полного ISO8601 с секундами
    ts_raw = meta.get("timestamp", "")
    try:
        # 20260717T153200Z -> парсим
        ts_iso = datetime.strptime(ts_raw, "%Y%m%dT%H%M%SZ").isoformat() if ts_raw else ""
    except ValueError:
        ts_iso = ts_raw

    signature = compute_signature(u.device_secret, file_path) if u.device_secret else ""

    # Поля multipart-формы (дублируем версию в теле — для надёжности)
    form_fields = {
        "device_id": u.device_id,
        "timestamp": ts_iso,
        "duration": str(cfg.recorder.segment_seconds),
        "voxnode_version": version,
    }
    if signature:
        form_fields["signature"] = signature

    headers = {
        "X-Voxnode-Version": version,
        "X-Voxnode-Device": u.device_id,
    }

    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                files={"file": (file_path.name, f, "audio/ogg")},
                data=form_fields,
                headers=headers,
                timeout=(u.connect_timeout, u.read_timeout),
            )
    except requests.RequestException as e:
        return False, f"network error: {e}"

    # 200/201/202 — успех
    if 200 <= resp.status_code < 300:
        return True, None

    # 4xx — постоянная ошибка (например, 400 bad signature). Логируем и удаляем,
    # чтобы не копить мусор. Кроме 408/429 — это retryable.
    if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
        return False, f"permanent server error {resp.status_code}: {resp.text[:200]}"

    # 5xx, 408, 429 — retryable
    return False, f"retryable server error {resp.status_code}: {resp.text[:200]}"


def move_to_spill(cfg: Config, file_path: Path) -> None:
    """Перенести файл в spill (SD), если RAM-буфер переполнен."""
    spill = Path(cfg.buffer.spill_dir)
    spill.mkdir(parents=True, exist_ok=True)
    try:
        target = spill / file_path.name
        file_path.rename(target)
        log.info("перемещён в spill (SD): %s -> %s", file_path.name, target)
    except OSError as e:
        log.error("не удалось переместить в spill: %s", e)


def run_uploader(cfg: Config | None = None) -> int:
    """Главная точка входа — постоянный цикл отправки."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = cfg or load_config()
    log.info(
        "uploader starting; server=%s device=%s buffer=%s",
        cfg.uploader.server_url or "(none)",
        cfg.uploader.device_id,
        cfg.buffer.ram_dir,
    )

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        log.info("получен сигнал %s, останавливаюсь...", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    consecutive_errors = 0

    while not stop:
        segments = find_ready_segments(cfg)
        if not segments:
            # Буфер пуст — спим, ничего не отправляем
            time.sleep(IDLE_INTERVAL_SEC)
            continue

        log.info("в буфере %d сегментов на отправку", len(segments))

        for seg in segments:
            if stop:
                break

            success, err = upload_one(cfg, seg)
            if success:
                try:
                    seg.unlink()
                    log.info("отправлен и удалён: %s", seg.name)
                    consecutive_errors = 0
                except OSError as e:
                    log.error("не удалось удалить %s: %s", seg, e)
            else:
                log.warning("провал отправки %s: %s", seg.name, err)
                consecutive_errors += 1

                # После max_retries подряд ошибок — переносим в spill, не блокируем очередь
                if consecutive_errors >= cfg.uploader.max_retries:
                    log.error("достигнут лимит retries (%d), переношу %s в spill",
                              cfg.uploader.max_retries, seg.name)
                    move_to_spill(cfg, seg)
                    consecutive_errors = 0

                # Backoff перед следующей попыткой (на след. проходе)
                backoff = min(
                    cfg.uploader.backoff_base ** consecutive_errors,
                    300,  # cap 5 минут
                )
                log.info("backoff %.1fс перед следующей попыткой", backoff)
                # Прерывистый sleep — реагируем на SIGTERM быстрее
                slept = 0
                while slept < backoff and not stop:
                    time.sleep(1)
                    slept += 1
                break  # выходим из for — начнём новый цикл сканирования


if __name__ == "__main__":
    sys.exit(run_uploader())
