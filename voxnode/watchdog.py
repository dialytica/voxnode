"""Watchdog — надзор за recorder'ом и буфером.

Запускается как постоянный демон. Каждые CHECK_INTERVAL_SEC секунд проверяет:

1. RECORDER ALIVE: есть ли в buffer файлы с mtime свежее MAX_RECORDER_AGE_SEC?
   - recorder.py пишет сегменты длиной segment_seconds (60с). Если за
     (segment_seconds * 2 + запас) не появилось ни одного нового файла —
     recorder завис, перезапускаем его через systemctl restart.
   - Частичная (.part) тоже считается (она обновляется во время записи).

2. BUFFER OFFLOAD: заполнен ли RAM-буфер (tmpfs) больше порога?
   - Если да — переносим самые старые готовые файлы в spill_dir (SD).
   - Таким образом RAM не переполняется, запись идёт дальше.
   - При восстановлении сети uploader заберёт и из RAM, и из spill.

3. DISK SPACE: есть ли место на SD под spill_dir?
   - При критическом заполнении — логируем ERROR (далее — política удаления
     самых старых записей, пока оставлено как TODO).

Никаких сетевых действий. Watchdog — локальный сторож.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from voxnode.config import Config, load_config
from voxnode.version import get_version

log = logging.getLogger("voxnode.watchdog")

# Интервал между проверками (секунды). 30с — достаточно реактивно, но не грузит CPU.
CHECK_INTERVAL_SEC = 30

# Допустимый возраст последнего файла в buffer (секунды).
# recorder пишет сегменты по 60с, значит свежий файл всегда моложе ~70с.
# Если последний файл старше — recorder завис.
MAX_RECORDER_AGE_SEC = 180  # 3 минуты (запас на fs/ffmpeg/сегментацию)

# Порог заполнения RAM-буфера для offload (доля от размера tmpfs, 0..1).
RAM_OFFLOAD_THRESHOLD = 0.80

# Сколько файлов переносить за один цикл offload (чтобы не блокировать надолго).
OFFLOAD_BATCH_SIZE = 10

# Критический порог свободного места на SD (доля, ниже которого — ERROR).
DISK_CRITICAL_FREE = 0.05  # 5%


def is_recorder_alive(cfg: Config) -> tuple[bool, float | None]:
    """Проверить, пишет ли recorder свежие файлы.

    Returns:
        (alive, age_of_newest_file_sec) — age=None, если файлов нет вообще.
    """
    ram = Path(cfg.buffer.ram_dir)
    if not ram.is_dir():
        return False, None

    newest_mtime = 0.0
    now = time.time()
    has_any_file = False

    for entry in ram.iterdir():
        if not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        has_any_file = True
        if mtime > newest_mtime:
            newest_mtime = mtime

    if not has_any_file:
        # Файлов нет вообще — это нормально в первые минуты после старта.
        # Считаем "живым", чтобы не перезапускать recorder без причины.
        return True, None

    age = now - newest_mtime
    return age <= MAX_RECORDER_AGE_SEC, age


def restart_recorder() -> bool:
    """Перезапустить voxnode-recorder через systemctl."""
    log.warning("recorder завис, перезапускаю через systemctl...")
    try:
        result = subprocess.run(
            ["systemctl", "restart", "voxnode-recorder.service"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info("recorder перезапущен")
            return True
        log.error("не удалось перезапустить recorder: %s", result.stderr)
        return False
    except subprocess.SubprocessError as e:
        log.error("ошибка systemctl restart: %s", e)
        return False


def get_ram_usage_pct(ram_dir: Path) -> float | None:
    """Доля заполнения tmpfs (0..1). None, если не tmpfs."""
    # /proc/mounts: ищем строку tmpfs с этим mountpoint
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return None
    mp_str = str(ram_dir)
    if not any(
        line.startswith("tmpfs ") and line.split()[1] == mp_str
        for line in mounts.splitlines()
    ):
        return None  # не tmpfs

    total, used, free = shutil.disk_usage(ram_dir)
    if total == 0:
        return 0.0
    return used / total


def offload_to_spill(cfg: Config) -> int:
    """Перенести самые старые готовые файлы из RAM-буфера в spill (SD).

    Пропускает .part (ещё пишутся). Возвращает число перенесённых файлов.
    """
    ram = Path(cfg.buffer.ram_dir)
    spill = Path(cfg.buffer.spill_dir)
    spill.mkdir(parents=True, exist_ok=True)

    # Собираем готовые файлы (не .part), сортируем по mtime (старые первыми)
    ready: list[tuple[float, Path]] = []
    for entry in ram.iterdir():
        if not entry.is_file():
            continue
        if entry.name.endswith(".part"):
            continue
        try:
            ready.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    ready.sort(key=lambda x: x[0])

    moved = 0
    for _, src in ready[:OFFLOAD_BATCH_SIZE]:
        dst = spill / src.name
        try:
            src.rename(dst)
            moved += 1
            log.info("offload: %s → spill", src.name)
        except OSError as e:
            log.error("не смог перенести %s: %s", src, e)
            break
    return moved


def check_disk_space(path: Path) -> tuple[bool, float]:
    """Проверить свободное место под path.

    Returns:
        (ok, free_fraction) — ok=False если меньше DISK_CRITICAL_FREE свободно.
    """
    total, used, free = shutil.disk_usage(path)
    if total == 0:
        return True, 0.0
    free_frac = free / total
    return free_frac >= DISK_CRITICAL_FREE, free_frac


def run_watchdog(cfg: Config | None = None) -> int:
    """Главная точка входа — постоянный цикл."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = cfg or load_config()
    log.info(
        "watchdog starting; version=%s check_interval=%ds ram_offload=%d%%",
        get_version(), CHECK_INTERVAL_SEC, int(RAM_OFFLOAD_THRESHOLD * 100),
    )

    ram_dir = Path(cfg.buffer.ram_dir)
    spill_dir = Path(cfg.buffer.spill_dir)

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        log.info("получен сигнал %s, останавливаюсь...", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Стартовый grace-период: не считаем recorder мёртвым первые минуты после
    # загрузки малины (даём ему время поднять ffmpeg и записать первый сегмент).
    boot_grace_until = time.time() + MAX_RECORDER_AGE_SEC + cfg.recorder.segment_seconds

    while not stop:
        try:
            # 1. Recorder alive?
            alive, age = is_recorder_alive(cfg)
            if not alive and time.time() > boot_grace_until:
                log.warning("recorder не пишет %.0f сек (последний файл %.0f сек назад)",
                            MAX_RECORDER_AGE_SEC, age or -1)
                restart_recorder()
            elif age is not None:
                log.debug("recorder OK: последний файл %.0f сек назад", age)

            # 2. RAM offload
            ram_pct = get_ram_usage_pct(ram_dir)
            if ram_pct is not None and ram_pct >= RAM_OFFLOAD_THRESHOLD:
                log.warning("RAM-буфер заполнен на %d%%, переношу в spill",
                            int(ram_pct * 100))
                moved = offload_to_spill(cfg)
                log.info("перенесено %d файлов в spill", moved)

            # 3. Disk space
            disk_ok, free_frac = check_disk_space(spill_dir)
            if not disk_ok:
                log.error("КРИТИЧНО: свободно %.1f%% на разделе spill (%s)",
                          free_frac * 100, spill_dir)
                # TODO: политика удаления самых старых записей

        except Exception:
            # Watchdog не должен падать — логируем и продолжаем
            log.exception("неожиданная ошибка в цикле watchdog")

        # Прерывистый сон — реагируем на SIGTERM быстрее
        slept = 0
        while slept < CHECK_INTERVAL_SEC and not stop:
            time.sleep(1)
            slept += 1

    log.info("watchdog остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(run_watchdog())
