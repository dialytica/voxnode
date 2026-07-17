#!/bin/sh
# tools/upgrade.sh — обновление voxnode (ohmyzsh-style)
#
# Что делает:
#   1. git pull --rebase с auto-stash локальных правок
#   2. Если HEAD изменился:
#      - pip install -e заново (новые/удалённые зависимости)
#      - валидация импорта voxnode (откат при провале)
#      - поочерёдный restart сервисов (recorder последним — меньше разрыв записи)
#      - сохраняет SHA в voxnode.lastVersion для changelog
#
# Вызывается:
#   - tools/check_for_upgrade.sh при автообновлении (systemd timer)
#   - `voxnode update` вручную (CLI)

set -e

# ==============================================================================
# Конфигурация
# ==============================================================================
VOXNODE_HOME="${VOXNODE_HOME:-/opt/voxnode}"
VOXNODE_USER="${VOXNODE_USER:-contai}"
VOXNODE_REPO="${VOXNODE_REPO:-dialytica/voxnode}"
VOXNODE_BRANCH="${VOXNODE_BRANCH:-main}"

# Сервисы в порядке перезапуска (recorder последним = минимум разрыв записи)
SERVICES="voxnode-portal voxnode-uploader voxnode-watchdog voxnode-recorder"

# ==============================================================================
# Хелперы
# ==============================================================================
log()  { printf '[upgrade] %s\n' "$*"; }
err()  { printf '[upgrade] ОШИБКА: %s\n' "$*" >&2; }

# ==============================================================================
# Прелюдия
# ==============================================================================
cd "$VOXNODE_HOME" || { err "не могу cd в $VOXNODE_HOME"; exit 1; }

if [ ! -d .git ]; then
    err "$VOXNODE_HOME — не git-репозиторий. Обновление невозможно."
    exit 1
fi

# ==============================================================================
# 1. Защита от потери локальных правок — autostash как ohmyzsh
# ==============================================================================
PREV_AUTOSTASH=$(sudo -u "$VOXNODE_USER" git config --get rebase.autoStash 2>/dev/null || true)
sudo -u "$VOXNODE_USER" git config rebase.autoStash true

cleanup() {
    # Восстанавливаем предыдущее значение autoStash
    case "$PREV_AUTOSTASH" in
        ""|false) sudo -u "$VOXNODE_USER" git config --unset rebase.autoStash 2>/dev/null || true ;;
        *) sudo -u "$VOXNODE_USER" git config rebase.autoStash "$PREV_AUTOSTASH" ;;
    esac
}
trap cleanup EXIT

# remote/branch берём из git-config (как ohmyzsh — fork-aware)
REMOTE=$(sudo -u "$VOXNODE_USER" git config voxnode.remote 2>/dev/null || echo origin)
BRANCH=$(sudo -u "$VOXNODE_USER" git config voxnode.branch 2>/dev/null || echo "$VOXNODE_BRANCH")

log "репозиторий: $REMOTE/$BRANCH"

# ==============================================================================
# 2. Запоминаем текущий HEAD
# ==============================================================================
LAST_COMMIT=$(sudo -u "$VOXNODE_USER" git rev-parse HEAD 2>/dev/null || echo "")
log "текущая версия: ${LAST_COMMIT:-неизвестна}"

# ==============================================================================
# 3. git fetch + pull --rebase
# ==============================================================================
log "получаю обновления..."
if ! sudo -u "$VOXNODE_USER" git fetch --quiet "$REMOTE" "$BRANCH"; then
    err "git fetch провалился. Проверьте сеть."
    exit 1
fi

log "применяю (pull --rebase)..."
if ! sudo -u "$VOXNODE_USER" git pull --quiet --rebase "$REMOTE" "$BRANCH"; then
    err "git pull --rebase провалился. Возможен конфликт."
    err "Разрешите вручную: cd $VOXNODE_HOME && sudo -u $VOXNODE_USER git rebase --abort"
    exit 1
fi

# ==============================================================================
# 4. Сравниваем HEAD
# ==============================================================================
NEW_COMMIT=$(sudo -u "$VOXNODE_USER" git rev-parse HEAD)

if [ "$LAST_COMMIT" = "$NEW_COMMIT" ]; then
    log "уже на последней версии (${NEW_COMMIT:0:7})."
    exit 0
fi

log "обновление: ${LAST_COMMIT:0:7} -> ${NEW_COMMIT:0:7}"

# ==============================================================================
# 5. Обновляем Python-зависимости + переустановка пакета
# ==============================================================================
log "обновляю Python-зависимости..."
if [ -f requirements.txt ]; then
    sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --quiet -r requirements.txt || {
        err "pip install requirements провалился. Откатываю git..."
        sudo -u "$VOXNODE_USER" git reset --hard "$LAST_COMMIT" 2>/dev/null || true
        exit 1
    }
fi

log "переустанавливаю voxnode как пакет..."
if ! sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --quiet --no-deps -e "$VOXNODE_HOME"; then
    err "pip install -e провалился. Откатываю git..."
    sudo -u "$VOXNODE_USER" git reset --hard "$LAST_COMMIT" 2>/dev/null || true
    exit 1
fi

# ==============================================================================
# 6. Валидация: новый код должен импортироваться
# ==============================================================================
log "валидация нового кода..."
if ! sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/python" -c "import voxnode" 2>/dev/null; then
    err "новый код не импортируется (import voxnode провалился). Откатываю git..."
    sudo -u "$VOXNODE_USER" git reset --hard "$LAST_COMMIT" 2>/dev/null || true
    sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --quiet --no-deps -e "$VOXNODE_HOME" 2>/dev/null || true
    exit 1
fi

# ==============================================================================
# 7. Обновляем systemd-юниты, если в новом коде они изменились
# ==============================================================================
if [ -d systemd ]; then
    for unit in systemd/*.service systemd/*.timer; do
        [ -f "$unit" ] || continue
        unit_name=$(basename "$unit")
        if ! diff -q "$unit" "/etc/systemd/system/$unit_name" >/dev/null 2>&1; then
            log "обновляю systemd-unit: $unit_name"
            cp "$unit" "/etc/systemd/system/$unit_name"
        fi
    done
    systemctl daemon-reload
fi

# ==============================================================================
# 8. Перезапуск сервисов (recorder последним)
# ==============================================================================
for svc in $SERVICES; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        log "перезапускаю $svc..."
        systemctl restart "$svc" 2>/dev/null || log "  (не запущен, пропускаю)"
    fi
done

# ==============================================================================
# 9. Запоминаем SHA для changelog (как ohmyzsh.lastVersion)
# ==============================================================================
if [ -n "$LAST_COMMIT" ]; then
    sudo -u "$VOXNODE_USER" git config voxnode.lastVersion "$LAST_COMMIT"
fi

log "✓ обновление завершено: ${NEW_COMMIT:0:7}"
exit 0
