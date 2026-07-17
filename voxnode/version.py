"""Определение версии voxnode.

Как и oh-my-zsh, voxnode не хранит VERSION-файл. Версия — это короткий SHA
текущего git-коммита в каталоге установки (по умолчанию /opt/voxnode).

Это позволяет:
  - uploader'у отправлять X-Voxnode-Version на сервер (видеть, кто на какой версии)
  - автообновлению сравнивать локальный SHA с удалённым через GitHub API
  - команде `voxnode changelog` показывать коммиты с прошлого обновления
"""

from __future__ import annotations

import functools
import os
import subprocess

# Каталог установки: при разработке — корень репозитория, на устройстве — /opt/voxnode.
# В install.sh выставляется VOXNODE_HOME=/opt/voxnode через systemd-окружение.
INSTALL_DIR = os.environ.get("VOXNODE_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Запасной SHA, если не в git-репозитории (например, запакировано без .git).
_FALLBACK_VERSION = "unknown"


@functools.lru_cache(maxsize=1)
def get_version() -> str:
    """Вернуть короткий SHA текущего коммита (например, 'a1b2c3d').

    Результат кешируется на всё время жизни процесса — git rev-parse не
    вызывается на каждый HTTP-запрос uploader'а. После автообновления сервисы
    перезапускаются, и кеш инвалидируется естественно.
    """
    try:
        result = subprocess.run(
            ["git", "-C", INSTALL_DIR, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return result.stdout.strip() or _FALLBACK_VERSION
    except (subprocess.SubprocessError, FileNotFoundError):
        return _FALLBACK_VERSION


def get_last_version() -> str | None:
    """Вернуть SHA, с которого обновились в последний раз.

    Хранится в git config 'voxnode.lastVersion' — как oh-my-zsh использует
    'oh-my-zsh.lastVersion'. None, если обновлений ещё не было.
    """
    try:
        result = subprocess.run(
            ["git", "-C", INSTALL_DIR, "config", "voxnode.lastVersion"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
