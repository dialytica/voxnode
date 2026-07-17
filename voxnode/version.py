"""Определение версии voxnode.

Две концепции версии, как у зрелых проектов:
  - SEMVER (тег)     — например 'v0.1.0'. Человекочитаемая версия релиза.
    Берётся через `git describe --tags --abbrev=0`. Используется в X-Voxnode-Version
    и для сравнения при автообновлении.
  - COMMIT SHA       — например 'a1b2c3d'. Точный коммит. Нужен для отладки и
    для случаев, когда код стоит между тегами (dev-режим).

Автообновление работает по тегам: на малине едет только код отмеченный тегом
(vX.Y.Z), а не произвольные коммиты main. Это защита от случайной поломки
production-парка.

Соглашение об именовании тегов: semver с префиксом 'v' — v0.1.0, v1.2.3.
Pre-release теги (v1.0.0-rc1, v0.2.0-beta) автоматически исключаются — малины
обновляются только на stable-релизы.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess

# Каталог установки: при разработке — корень репозитория, на устройстве — /opt/voxnode.
# В install.sh выставляется VOXNODE_HOME=/opt/voxnode через systemd-окружение.
INSTALL_DIR = os.environ.get("VOXNODE_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Запасные значения, если не в git-репозитории.
_FALLBACK_VERSION = "unknown"
_FALLBACK_TAG = None

# Regex semver с префиксом 'v': v1.2.3, v0.1.0 (без pre-release — те исключаем)
_SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


@functools.lru_cache(maxsize=1)
def get_commit_sha(short: bool = True) -> str:
    """Вернуть SHA текущего коммита.

    Args:
        short: True → короткий SHA (a1b2c3d), False → полный.
    """
    args = ["git", "-C", INSTALL_DIR, "rev-parse"]
    args.append("--short" if short else "HEAD")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3, check=True)
        return result.stdout.strip() or _FALLBACK_VERSION
    except (subprocess.SubprocessError, FileNotFoundError):
        return _FALLBACK_VERSION


@functools.lru_cache(maxsize=1)
def get_tag() -> str | None:
    """Вернуть ближайший тег на текущем коммите (например 'v0.1.0').

    Возвращает None, если текущий коммит не отмечен тегом — это значит,
    мы на dev-коммите между релизами. В этом случае для X-Voxnode-Version
    нужно использовать get_version(), который отдаст SHA.
    """
    try:
        # --abbrev=0 = только точный тег на коммите, без суффиксов -N-gSHA
        result = subprocess.run(
            ["git", "-C", INSTALL_DIR, "describe", "--tags", "--exact-match", "--abbrev=0"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        tag = result.stdout.strip()
        return tag or None
    except subprocess.SubprocessError:
        return None


def get_version() -> str:
    """Человекочитаемая версия для заголовков и логов.

    Приоритет: точный тег > SHA. Если мы на теге vX.Y.Z — отдаём его.
    Если между тегами (dev) — отдаём SHA.

    Это значение уходит в X-Voxnode-Version на каждый upload. Сервер dialytica
    видит либо 'v1.2.3' (релиз), либо 'a1b2c3d' (dev-коммит на данной малине).
    """
    tag = get_tag()
    if tag:
        return tag
    return get_commit_sha()


def get_version_detail() -> dict[str, str | None]:
    """Полная информация о версии — для CLI `voxnode version`."""
    return {
        "tag": get_tag(),
        "sha": get_commit_sha(),
        "sha_full": get_commit_sha(short=False),
        "display": get_version(),
    }


def get_last_version() -> str | None:
    """Вернуть тег/SHA, с которого обновились в последний раз.

    Хранится в git config 'voxnode.lastVersion' — как oh-my-zsh использует
    'oh-my-zsh.lastVersion'. None, если обновлений ещё не было.
    """
    try:
        result = subprocess.run(
            ["git", "-C", INSTALL_DIR, "config", "voxnode.lastVersion"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def parse_semver(tag: str) -> tuple[int, int, int] | None:
    """Распарсить тег semver → (major, minor, patch). None если не semver."""
    m = _SEMVER_RE.match(tag)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer_tag(remote_tag: str, local_tag: str | None) -> bool:
    """ True если remote_tag новее local_tag (по semver).

    Если local_tag None — любой валидный remote_tag считается новее.
    Если хоть один не semver — сравнение по строке (лексикографически).
    """
    if local_tag is None:
        return True
    r = parse_semver(remote_tag)
    l = parse_semver(local_tag)
    if r is None or l is None:
        # Хотя бы один не semver — лексикографическое сравнение
        return remote_tag > local_tag
    return r > l
