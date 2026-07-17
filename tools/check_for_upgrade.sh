#!/bin/sh
# tools/check_for_upgrade.sh — проверка наличия обновления (ohmyzsh-style)
#
# Запускается systemd-timer'ом раз в день. Делает дешёвый pre-check через
# GitHub REST API: сравнивает remote HEAD SHA с локальным. Только если
# отличаются — вызывает upgrade.sh для тяжёлого git pull + restart.
#
# Защита от параллельного запуска: lock-директория (как ohmyzsh
# $ZSH/log/update.lock) с автоочисткой через 24 часа.

set -e

VOXNODE_HOME="${VOXNODE_HOME:-/opt/voxnode}"
VOXNODE_REPO="${VOXNODE_REPO:-dialytica/voxnode}"
VOXNODE_BRANCH="${VOXNODE_BRANCH:-main}"
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
# Сначала чистим устаревший lock
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
# 2. Локальный HEAD
# ==============================================================================
LOCAL_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -z "$LOCAL_SHA" ]; then
    err "не могу определить локальный HEAD"
    exit 0
fi

# ==============================================================================
# 3. Дешёвый pre-check через GitHub REST API (timeout 5 сек)
# ==============================================================================
# Запрашиваем только SHA последнего коммита ветки
API_URL="https://api.github.com/repos/${VOXNODE_REPO}/commits/${VOXNODE_BRANCH}"

REMOTE_SHA=$(
    curl --connect-timeout 5 --max-time 10 -fsSL \
         -H 'Accept: application/vnd.github.v3.sha' \
         "$API_URL" 2>/dev/null | tr -d '[:space:]'
)

if [ -z "$REMOTE_SHA" ]; then
    # Сеть недоступна или GitHub недоступен — тихо выходим, попробуем в следующий раз
    log "не удалось получить remote SHA (сеть/GitHub недоступен)"
    exit 0
fi

log "local:  $(echo "$LOCAL_SHA" | cut -c1-7)"
log "remote: $(echo "$REMOTE_SHA" | cut -c1-7)"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    log "уже актуально"
    exit 0
fi

# ==============================================================================
# 4. Есть обновление — запускаем upgrade.sh
# ==============================================================================
log "обнаружено обновление, запускаю upgrade.sh"
UPGRADE_SCRIPT="$VOXNODE_HOME/tools/upgrade.sh"
if [ ! -x "$UPGRADE_SCRIPT" ]; then
    err "$UPGRADE_SCRIPT не найден или не исполняемый"
    exit 1
fi

sh "$UPGRADE_SCRIPT"
