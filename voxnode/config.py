"""Загрузка и валидация конфигурации voxnode.

Конфиг живёт в /etc/voxnode/config.yaml (на устройстве) или config/config.yaml
(при локальной разработке). Структура описана в config/config.example.yaml.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

# Где искать конфиг (порядок имеет значение — первый найденный выигрывает).
_CONFIG_SEARCH_PATHS = [
    Path(os.environ.get("VOXNODE_CONFIG", "")),  # явный override
    Path("/etc/voxnode/config.yaml"),             # устройство (install.sh кладёт сюда)
    Path("config/config.yaml"),                   # локальная разработка
    Path("config/config.example.yaml"),           # запасной — пример
]


@dataclasses.dataclass
class RecorderConfig:
    """Параметры записи."""

    device: str = "default"            # ALSA-имя карты, например "plughw:CARD=3800"
    sample_rate: int = 16000           # 16 kHz — родная частота XVF3800, достаточно для речи
    channels: int = 1                  # моно (сводим стерео из XVF3800 в моно)
    segment_seconds: int = 60          # длина одного сегмента
    format: str = "opus"               # ffmpeg-кодек (Opus оптимален для речи: ~24 kbps)


@dataclasses.dataclass
class UploaderConfig:
    """Параметры отправки на сервер."""

    server_url: str = ""               # например, https://api.dialytica.ru
    device_id: str = "voxnode-unknown" # идентификатор устройства
    device_secret: str = ""            # секрет для HMAC-подписи
    upload_endpoint: str = "/api/v1/audio/upload"
    connect_timeout: int = 10          # секунды
    read_timeout: int = 60             # секунды (большой файл + медленная сеть)
    max_retries: int = 5               # максимум попыток для одного файла
    backoff_base: float = 2.0          # база экспоненциального backoff (2^n секунд)


@dataclasses.dataclass
class BufferConfig:
    """Параметры буфера (tmpfs + spill)."""

    ram_dir: str = "/var/voxnode/buffer"    # tmpfs, RAM
    spill_dir: str = "/var/voxnode/spill"   # SD, используется при переполнении RAM
    ram_max_mb: int = 512                   # порог offload на SD (watchdog следит)


@dataclasses.dataclass
class Config:
    """Полный конфиг voxnode."""

    recorder: RecorderConfig = dataclasses.field(default_factory=RecorderConfig)
    uploader: UploaderConfig = dataclasses.field(default_factory=UploaderConfig)
    buffer: BufferConfig = dataclasses.field(default_factory=BufferConfig)


def find_config_path() -> Path | None:
    """Вернуть путь к первому существующему конфиг-файлу или None."""
    for path in _CONFIG_SEARCH_PATHS:
        if path.is_file():
            return path
    return None


def load_config(path: Path | str | None = None) -> Config:
    """Загрузить и распарсить конфиг.

    Args:
        path: явный путь к YAML. Если None — ищется автоматически.

    Raises:
        FileNotFoundError: конфиг не найден ни в одном из стандартных мест.
        ValueError: конфиг есть, но невалидный YAML или нет обязательных полей.
    """
    if path is None:
        found = find_config_path()
        if found is None:
            raise FileNotFoundError(
                "Конфиг не найден. Создай /etc/voxnode/config.yaml "
                "из config/config.example.yaml"
            )
        path = found
    else:
        path = Path(path)

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return _build_config(raw)


def _build_config(raw: dict[str, Any]) -> Config:
    """Собрать dataclass Config из сырого dict."""
    recorder_raw = raw.get("recorder", {}) or {}
    uploader_raw = raw.get("uploader", {}) or {}
    buffer_raw = raw.get("buffer", {}) or {}

    # server_url — единственное строго обязательное поле. Без него uploader
    # не сможет работать (но recorder может — просто копит в буфер).
    server_url = str(uploader_raw.get("server_url", "")).strip()
    if not server_url:
        # Не падаем — устройство может работать в режиме накопления.
        # Uploader будет логировать warning и ждать.
        pass

    return Config(
        recorder=RecorderConfig(
            device=str(recorder_raw.get("device", RecorderConfig.device)),
            sample_rate=int(recorder_raw.get("sample_rate", RecorderConfig.sample_rate)),
            channels=int(recorder_raw.get("channels", RecorderConfig.channels)),
            segment_seconds=int(recorder_raw.get("segment_seconds", RecorderConfig.segment_seconds)),
            format=str(recorder_raw.get("format", RecorderConfig.format)),
        ),
        uploader=UploaderConfig(
            server_url=server_url,
            device_id=str(uploader_raw.get("device_id", UploaderConfig.device_id)),
            device_secret=str(uploader_raw.get("device_secret", "")),
            upload_endpoint=str(uploader_raw.get("upload_endpoint", UploaderConfig.upload_endpoint)),
            connect_timeout=int(uploader_raw.get("connect_timeout", UploaderConfig.connect_timeout)),
            read_timeout=int(uploader_raw.get("read_timeout", UploaderConfig.read_timeout)),
            max_retries=int(uploader_raw.get("max_retries", UploaderConfig.max_retries)),
            backoff_base=float(uploader_raw.get("backoff_base", UploaderConfig.backoff_base)),
        ),
        buffer=BufferConfig(
            ram_dir=str(buffer_raw.get("ram_dir", BufferConfig.ram_dir)),
            spill_dir=str(buffer_raw.get("spill_dir", BufferConfig.spill_dir)),
            ram_max_mb=int(buffer_raw.get("ram_max_mb", BufferConfig.ram_max_mb)),
        ),
    )
