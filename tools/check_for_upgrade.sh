#!/bin/sh
# tools/check_for_upgrade.sh — проверка наличия обновления по тегам релизов
#
# Запускается systemd-timer'ом раз в день.
#
# Логика:
#   - GitHub REST API /releases/latest даёт последний STABLE-тег (без pre-release)
#   - Сравниваем с локальным тегом (git describe --exact-match)
#   - Если remote-тег новее (semver) → запускаем upgrade.sh
#   - Если мы на dev-коммите без тега → пропускаем (не откатываемся к тегу автоматически)
#
# Защита от параллельного запуска: lock-директория с автоочисткой через 24 часа.

set -e

VOXNODE_HOME="${VOXNODE_HOME:-/opt/voxnode}"
VOXNODE_REPO="${VOXNODE_REPO:-dialytica/voxnode}"
LOCK_DIR="$VOXNODE_HOME/.update.lock"
LOCK_MAX_AGE_SEC=86400  # 24 часа

log()  { printf '[check] %s\n' "$*"; }
err()  { printf '[check] %s\n' "$*" >&2; }

# ==============================================================================
# 0. Pre-flight: git есть, репозиторий валиден
# ==============================================================================
command -v git >/dev/null 2>&1 || { err "git не найден"; exit 0; }
cd "$VOXNODE_HOME" 2>/dev/null || { err "$VOXNODE_HOME недоступен"; exit 0; }
[ -d .git ] || { err "не git-репозиторий"; exit 0; }

# ==============================================================================
# 1. Lock-директория (атомарная операция mkdir)
# ==============================================================================
if [ -d "$LOCK_DIR" ]; then
    lock_age=$(($(date +%s) - $(stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0)))
    if [ "$lock_age" -gt "$LOCK_MAX_AGE_SEC" ]; then
        log "удаляю устаревший lock ($lock_age сек)"
        rm -rf "$LOCK_DIR" 2>/dev/null || true
    else
        log "другой update уже идёт, выхожу"
        exit 0
    fi
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "lock занят, выхожу"
    exit 0
fi

cleanup() {
    rm -rf "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# ==============================================================================
# 2. Локальный тег (точный тег на текущем коммите)
# ==============================================================================
LOCAL_TAG=$(git describe --tags --exact-match --abbrev=0 2>/dev/null || echo "")
LOCAL_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

if [ -z "$LOCAL_TAG" ]; then
    # Мы на dev-коммите между релизами. НЕ пытаемся обновляться автоматически —
    # dev-малина остаётся на своём коммите до тех пор, пока кто-то явно не
    # поставит тег или не запустит `voxnode update --force` (заглушка).
    log "локально: dev-коммит $LOCAL_SHA (нет тега) — автообновление пропущено"
    exit 0
fi

log "локально: $LOCAL_TAG ($LOCAL_SHA)"

# ==============================================================================
# 3. GitHub API: последний stable-релиз
# ==============================================================================
API_URL="https://api.github.com/repos/${VOXNODE_REPO}/releases/latest"

# Запрос с коротким timeout — не блокируем загрузку малины надолго
RESPONSE=$(curl --connect-timeout 5 --max-time 10 -fsSL \
                -H 'Accept: application/vnd.github+json' \
                "$API_URL" 2>/dev/null || echo "")

if [ -z "$RESPONSE" ]; then
    log "не удалось получить releases/latest (сеть/GitHub недоступен)"
    exit 0
fi

# Извлекаем tag_name из JSON. Python надёжнее, чем grep для JSON.
REMOTE_TAG=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tag_name', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

if [ -z "$REMOTE_TAG" ]; then
    err "не удалось распарсить tag_name из ответа API"
    exit 0
fi

log "удалённо: $REMOTE_TAG"

# ==============================================================================
# 4. Сравнение semver
# ==============================================================================
# Если теги совпадают — обновляться не на что
if [ "$LOCAL_TAG" = "$REMOTE_TAG" ]; then
    log "уже актуально ($LOCAL_TAG)"
    exit 0
fi

# Semver-сравнение через Python (надёжнее, чем bash)
SHOULD_UPDATE=$(python3 -c "
import re
def parse(t):
    m = re.match(r'^v(\d+)\.(\d+)\.(\d+)\$', t)
    return tuple(int(x) for x in m.groups()) if m else None
remote = parse('$REMOTE_TAG')
local = parse('$LOCAL_TAG')
if remote is None or local is None:
    # Хоть один не semver — лексикографическое сравнение
    print('1' if '$REMOTE_TAG' > '$LOCAL_TAG' else '0')
else:
    print('1' if remote > local else '0')
" 2>/dev/null || echo "0")

if [ "$SHOULD_UPDATE" != "1" ]; then
    log "локальный тег новее или равен remote — пропускаю"
    exit 0
fi

# ==============================================================================
# 5. Есть новый релиз — запускаем upgrade.sh
# ==============================================================================
log "обнаружен новый релиз: $LOCAL_TAG → $REMOTE_TAG"
log "запускаю upgrade.sh"
UPGRADE_SCRIPT="$VOXNODE_HOME/tools/upgrade.sh"
if [ ! -f "$UPGRADE_SCRIPT" ]; then
    err "$UPGRADE_SCRIPT не найден"
    exit 1
fi

# Передаём целевой тег через env — upgrade.sh подхватит VOXNODE_TARGET_TAG
export VOXNODE_TARGET_TAG="$REMOTE_TAG"
sh "$UPGRADE_SCRIPT"
