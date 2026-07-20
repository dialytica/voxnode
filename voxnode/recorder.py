"""Непрерывная запись аудио минутными сегментами через ffmpeg.

Каждый сегмент = отдельный файл с timestamp в имени. Файлы пишутся в
RAM-буфер (tmpfs /var/voxnode/buffer). Uploader забирает их оттуда.

Имя файла: {device_id}_{YYYYmmddTHHMMSSZ}.{ext}
    например: shop-moscow-01_20260717T153200Z.opus

Формат: Opus 16kHz mono — ~1.7 MB/час, оптимально для речи.

Стратегия: запускаем ffmpeg один раз с segment muxer, который сам ротит файлы
каждые segment_seconds. Это надёжнее цикла «запусти ffmpeg на 60 сек, жди,
перезапусти» — нет разрыва между сегментами и нет риска зависнуть на рестарте.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from voxnode.config import Config, load_config

log = logging.getLogger("voxnode.recorder")

# Префикс для частичных (пишущихся сейчас) файлов — uploader должен их пропускать.
_PARTIAL_SUFFIX = ".part"


def segment_filename(device_id: str, fmt: str, when: datetime | None = None) -> str:
    """Сгенерировать имя для нового сегмента.

    ISO-8601 basic (T и Z без разделителей) — хорошо сортируется лексикографически
    и читается человеком.
    """
    when = when or datetime.now(timezone.utc)
    ts = when.strftime("%Y%m%dT%H%M%SZ")
    return f"{device_id}_{ts}.{fmt}"


def build_ffmpeg_cmd(cfg: Config, out_pattern: str) -> list[str]:
    """Собрать команду ffmpeg для непрерывной сегментированной записи.

    Args:
        cfg: конфиг voxnode
        out_pattern: путь-шаблон вывода (ffmpeg подставит %Y%m%d и т.п. или сегмент-номер)

    Используем segment muxer с time-based segmenting. Opus оптимален для речи.

    Важный нюанс ALSA: устройство может отдавать только свои нативные каналы
    (например ReSpeaker XVF3800 = строго 2 канала). Запрашивать у ALSA 1 канал
    бесполезно — она не умеет сводить. Поэтому:
      - Захват: всегда channels=2 (натив ReSpeaker), ALSA откроет устройство.
      - Сведение в моно: через ffmpeg audio filter 'pan=mono|c0=c1' (берём
        канал ASR/beam как более качественный для речи, канал 1 в XVF3800).

    Note: ALSA-источник через '-f alsa -i <device>'. Для ReSpeaker XVF3800
    device имеет вид 'hw:Array' (число нестабильно при подключении доп. карт).
    """
    r = cfg.recorder
    # Сколько каналов захватывать из ALSA (натив устройства, не путать с output)
    alsa_channels = 2

    # Если целевой channels=1 — сводим через pan filter в моно, беря канал 1
    # (ASR/beam XVF3800 = более разборчивая речь, чем канал 0 Conference).
    # Если channels=2 — оставляем стерео как есть.
    if r.channels == 1:
        audio_filter = ["-af", "pan=mono|c0=c1"]
    else:
        audio_filter = []

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",          # тише в логах (errors всё равно покажет)
        "-nostdin",                       # не красть stdin у сервиса
        # --- Вход: ALSA ---
        "-f", "alsa",
        "-sample_rate", str(r.sample_rate),
        "-channels", str(alsa_channels),
        "-i", r.device,
    ]
    cmd += audio_filter
    cmd += [
        # --- Кодирование: libopus для речи ---
        "-c:a", "libopus",
        "-b:a", "24k",                    # 24 kbps достаточно для речи 16kHz mono
        "-application", "voip",           # режим Opus, оптимизированный под речь
        "-ar", str(r.sample_rate),
        "-ac", str(r.channels),
        # --- Segment muxer: ротация по времени ---
        "-f", "segment",
        "-segment_time", str(r.segment_seconds),
        "-segment_format", "ogg",         # Opus контейнер = OGG
        "-reset_timestamps", "1",
        "-strftime", "1",                 # разрешить %Y%m%d... в имени файла
        # Записываем сначала в .part, потом uploader игнорирует .part
        out_pattern,
    ]
    return cmd


def _out_pattern(cfg: Config) -> str:
    """Шаблон пути для segment muxer'а.

    ffmpeg с -strftime 1 подставляет strftime-коды. Добавляем .part, чтобы
    uploader не забрал пишущийся файл.
    """
    device_id = cfg.uploader.device_id
    fmt = cfg.recorder.format
    # %Y%m%dT%H%M%SZ -> UTC время старта сегмента (системные часы в UTC на Pi)
    name = f"{device_id}_%Y%m%dT%H%M%SZ.{fmt}{_PARTIAL_SUFFIX}"
    return str(Path(cfg.buffer.ram_dir) / name)


def run_recorder(cfg: Config | None = None) -> int:
    """Главная точка входа. Запускает ffmpeg и держит его живым.

    При падении ffmpeg — перезапуск через 2 секунды (защита от transient errors).
    Обработка SIGTERM/SIGINT — корректное завершение.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = cfg or load_config()
    ram_dir = Path(cfg.buffer.ram_dir)
    ram_dir.mkdir(parents=True, exist_ok=True)

    pattern = _out_pattern(cfg)
    log.info("recorder starting; device=%s rate=%d out_dir=%s",
             cfg.recorder.device, cfg.recorder.sample_rate, ram_dir)

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        log.info("получен сигнал %s, останавливаюсь...", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop:
        cmd = build_ffmpeg_cmd(cfg, pattern)
        log.info("запускаю ffmpeg: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd)

            # Ждём либо завершения ffmpeg, либо сигнала остановки.
            while not stop:
                rc = proc.poll()
                if rc is not None:
                    break
                time.sleep(1)

            if stop:
                log.info("отправляю SIGTERM в ffmpeg (pid=%d)", proc.pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning("ffmpeg не завершился за 5с, SIGKILL")
                    proc.kill()
                break

            # ffmpeg упал сам — логируем и перезапускаем
            log.error("ffmpeg завершился с rc=%s, перезапуск через 2с", rc)
            time.sleep(2)

        except FileNotFoundError:
            log.critical("ffmpeg не найден в PATH. Установите: sudo apt install ffmpeg")
            return 2
        except Exception:
            log.exception("неожиданная ошибка в цикле recorder")
            time.sleep(5)

    log.info("recorder остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(run_recorder())
