#!/bin/sh
# tools/install.sh — ohmyzsh-style one-line installer для voxnode
#
# Использование (curl | sh):
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/dialytica/voxnode/main/tools/install.sh)"
#
# или wget:
#   sh -c "$(wget -qO- https://raw.githubusercontent.com/dialytica/voxnode/main/tools/install.sh)"
#
# Что делает:
#   1. Клонирует voxnode в /opt/voxnode (shallow clone, git init + fetch --depth=1)
#   2. Создаёт venv, ставит Python-зависимости
#   3. Создаёт /etc/voxnode/config.yaml из примера (только если его нет)
#   4. Создаёт каталоги буфера, монтирует tmpfs
#   5. Копирует и включает systemd-юниты
#
# Не перезаписывает существующий /opt/voxnode — для обновления используйте
# `voxnode update` или tools/upgrade.sh.

set -e

# ==============================================================================
# Конфигурация (перекрывается переменными окружения)
# ==============================================================================
VOXNODE_HOME="${VOXNODE_HOME:-/opt/voxnode}"
VOXNODE_REPO="${VOXNODE_REPO:-dialytica/voxnode}"
VOXNODE_REMOTE="${VOXNODE_REMOTE:-https://github.com/${VOXNODE_REPO}.git}"
VOXNODE_BRANCH="${VOXNODE_BRANCH:-main}"

VOXNODE_USER="${VOXNODE_USER:-contai}"
VOXNODE_CONFIG_DIR="${VOXNODE_CONFIG_DIR:-/etc/voxnode}"
VOXNODE_VAR_DIR="${VOXNODE_VAR_DIR:-/var/voxnode}"

# Размер tmpfs для RAM-буфера (по умолчанию 512M — ~2 дня Opus offline)
VOXNODE_TMPFS_SIZE="${VOXNODE_TMPFS_SIZE:-512M}"

# ==============================================================================
# Хелперы
# ==============================================================================
fmt_red()    { printf '\033[31m%s\033[0m\n' "$*"; }
fmt_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
fmt_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
fmt_blue()   { printf '\033[34m%s\033[0m\n' "$*"; }

error()  { fmt_red "✗ $*" >&2; }
info()   { fmt_blue "→ $*"; }
ok()     { fmt_green "✓ $*"; }
warn()   { fmt_yellow "⚠ $*"; }

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        error "Этот скрипт должен запускаться от root (используйте sudo)."
        exit 1
    fi
}

# ==============================================================================
# 0. Прелюдия
# ==============================================================================
cat <<'BANNER'
                _           _
  __ _ _ __ __ _| |_ _   _  (_)_______
 / _` | '__/ _` | __| | | || |_  / _ \\
| (_| | | | (_| | |_| |_| || |/ /  __/
 \__,_|_|  \__,_|\__|\__,_|/ /___\\___|
                        |__|

  Mass-deployable audio recording agent for Raspberry Pi
  Part of the dialytica project
BANNER
printf '\n'

# ==============================================================================
# 1. Проверки зависимостей
# ==============================================================================
info "Проверка зависимостей..."

MISSING=""
for cmd in git python3 ffmpeg arecord; do
    if ! command_exists "$cmd"; then
        MISSING="$MISSING $cmd"
    fi
done

if [ -n "$MISSING" ]; then
    error "Не найдены команды:$MISSING"
    info  "Установите их системно:"
    echo  "  sudo apt-get update && sudo apt-get install -y git python3 python3-venv ffmpeg alsa-utils"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    error "Нужен Python 3.9+, найден $PYTHON_VERSION"
    exit 1
fi
ok "Зависимости OK (Python $PYTHON_VERSION)"

# ==============================================================================
# 2. Проверка ОС (предупреждение, не блокировка)
# ==============================================================================
if [ -f /etc/os-release ]; then
    . /etc/os-release
    info "ОС: $PRETTY_NAME"
fi
if [ ! -f /proc/device-tree/model ] && ! grep -q "Raspberry Pi\|raspberrypi" /proc/device-tree/model 2>/dev/null; then
    warn "Это не похоже на Raspberry Pi — voxnode разработан для Pi OS."
    warn "Продолжаю, но гарантий нет."
fi

# ==============================================================================
# 3. Проверка root
# ==============================================================================
require_root

# ==============================================================================
# 4. Проверка существующей установки (как ohmyzsh — отказ, не перезапись)
# ==============================================================================
if [ -d "$VOXNODE_HOME/.git" ]; then
    warn "Каталог $VOXNODE_HOME уже содержит voxnode."
    echo  "  Чтобы обновить, выполните: voxnode update"
    echo  "  Чтобы переустановить, удалите: sudo rm -rf $VOXNODE_HOME"
    exit 1
fi

# ==============================================================================
# 5. Клонирование репозитория (как ohmyzsh: git init + fetch --depth=1)
# ==============================================================================
info "Клонирую voxnode в $VOXNODE_HOME (ветка $VOXNODE_BRANCH)..."

# Создаём каталог, выставляем владельца
mkdir -p "$VOXNODE_HOME"
chown "$VOXNODE_USER:$VOXNODE_USER" "$VOXNODE_HOME"

# Shallow clone через sudo -u (как ohmyzsh, но от root-установщика)
sudo -u "$VOXNODE_USER" git init --quiet "$VOXNODE_HOME" || {
    error "git init не удался"
    rm -rf "$VOXNODE_HOME"
    exit 1
}

cd "$VOXNODE_HOME" || exit 1

# Конфиг-ключи как ohmyzsh — сохраняем намерение установки для автообновлений
sudo -u "$VOXNODE_USER" git config core.eol lf
sudo -u "$VOXNODE_USER" git config core.autocrlf false
sudo -u "$VOXNODE_USER" git config voxnode.remote origin
sudo -u "$VOXNODE_USER" git config voxnode.branch "$VOXNODE_BRANCH"
sudo -u "$VOXNODE_USER" git remote add origin "$VOXNODE_REMOTE" || true

if ! sudo -u "$VOXNODE_USER" git fetch --depth=1 origin "$VOXNODE_BRANCH"; then
    error "git fetch не удался. Проверьте сеть и репозиторий $VOXNODE_REMOTE"
    rm -rf "$VOXNODE_HOME"
    exit 1
fi

sudo -u "$VOXNODE_USER" git checkout -b "$VOXNODE_BRANCH" "origin/$VOXNODE_BRANCH" || {
    error "git checkout не удался"
    rm -rf "$VOXNODE_HOME"
    exit 1
}

# Локальный git identity (для будущих autostash при обновлениях)
sudo -u "$VOXNODE_USER" git -C "$VOXNODE_HOME" config user.name "dialytica-voxnode"
sudo -u "$VOXNODE_USER" git -C "$VOXNODE_HOME" config user.email "voxnode@dialytica.local"

ok "Клонирование завершено"

# ==============================================================================
# 6. Python venv и зависимости
# ==============================================================================
info "Создаю venv и ставлю Python-зависимости..."
sudo -u "$VOXNODE_USER" python3 -m venv "$VOXNODE_HOME/.venv"
sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --quiet -r "$VOXNODE_HOME/requirements.txt"
ok "Python-зависимости установлены"

# ==============================================================================
# 6b. Устанавливаем voxnode как editable-пакет — pip создаст CLI voxnode
# ==============================================================================
info "Устанавливаю voxnode как пакет (это создаст CLI)..."
if ! sudo -u "$VOXNODE_USER" "$VOXNODE_HOME/.venv/bin/pip" install --no-deps -e "$VOXNODE_HOME"; then
    error "pip install -e провалился. Проверьте целостность pyproject.toml."
    exit 1
fi
ok "Пакет voxnode установлен"

# ==============================================================================
# 7. Конфигурация (/etc/voxnode/config.yaml — не трогаем существующий)
# ==============================================================================
info "Настраиваю /etc/voxnode/..."
mkdir -p "$VOXNODE_CONFIG_DIR"
chown "$VOXNODE_USER:$VOXNODE_USER" "$VOXNODE_CONFIG_DIR"

if [ ! -f "$VOXNODE_CONFIG_DIR/config.yaml" ]; then
    cp "$VOXNODE_HOME/config/config.example.yaml" "$VOXNODE_CONFIG_DIR/config.yaml"
    chown "$VOXNODE_USER:$VOXNODE_USER" "$VOXNODE_CONFIG_DIR/config.yaml"
    ok "Создан $VOXNODE_CONFIG_DIR/config.yaml из шаблона"
else
    ok "config.yaml уже существует — оставляю как есть"
fi

# ==============================================================================
# 8. Каталоги буфера + tmpfs
# ==============================================================================
info "Настраиваю буфер (/var/voxnode)..."
mkdir -p "$VOXNODE_VAR_DIR/buffer" "$VOXNODE_VAR_DIR/spill"
chown -R "$VOXNODE_USER:$VOXNODE_USER" "$VOXNODE_VAR_DIR"

# tmpfs для RAM-буфера — пишем в /etc/fstab если ещё не туда
FSTAB_LINE="tmpfs $VOXNODE_VAR_DIR/buffer tmpfs defaults,size=$VOXNODE_TMPFS_SIZE,nodev,nosuid,mode=0755,uid=$(id -u $VOXNODE_USER),gid=$(id -g $VOXNODE_USER) 0 0"
if ! grep -q "^tmpfs $VOXNODE_VAR_DIR/buffer " /etc/fstab 2>/dev/null; then
    echo "$FSTAB_LINE" >> /etc/fstab
    ok "tmpfs ($VOXNODE_TMPFS_SIZE) добавлен в /etc/fstab"
fi
mount "$VOXNODE_VAR_DIR/buffer" 2>/dev/null || true
ok "Буфер готов: $VOXNODE_VAR_DIR/buffer (RAM) + $VOXNODE_VAR_DIR/spill (SD)"

# ==============================================================================
# 9. Systemd-юниты
# ==============================================================================
info "Устанавливаю systemd-юниты..."
for unit in voxnode-recorder.service voxnode-uploader.service; do
    if [ -f "$VOXNODE_HOME/systemd/$unit" ]; then
        cp "$VOXNODE_HOME/systemd/$unit" "/etc/systemd/system/$unit"
    fi
done
systemctl daemon-reload
ok "systemd-юниты установлены"

# ==============================================================================
# 10. CLI-симлинк /usr/local/bin/voxnode -> venv voxnode
# ==============================================================================
info "Создаю системный CLI /usr/local/bin/voxnode..."
mkdir -p /usr/local/bin
# pip install -e уже создал /opt/voxnode/.venv/bin/voxnode. Делаем системный симлинк,
# который выставляет VOXNODE_HOME/VOXNODE_CONFIG.
cat > /usr/local/bin/voxnode <<VOXNODE_CLI
#!/bin/sh
# voxnode CLI — прокси к venv entry point с правильным окружением
export VOXNODE_HOME=$VOXNODE_HOME
export VOXNODE_CONFIG=$VOXNODE_CONFIG_DIR/config.yaml
exec $VOXNODE_HOME/.venv/bin/voxnode "\$@"
VOXNODE_CLI
chmod +x /usr/local/bin/voxnode
ok "CLI доступен: voxnode"

# ==============================================================================
# 11. Запуск сервисов
# ==============================================================================
info "Включаю и запускаю сервисы..."
systemctl enable --now voxnode-recorder.service 2>/dev/null || warn "recorder не запустился (нормально, если микрофон ещё не подключён)"
ok "Сервисы включены"

# ==============================================================================
# 12. Финал
# ==============================================================================
CURRENT_VER=$(sudo -u "$VOXNODE_USER" git -C "$VOXNODE_HOME" rev-parse --short HEAD 2>/dev/null || echo "unknown")

cat <<EOF

$(fmt_green '✓ voxnode установлен успешно!')

  Версия:      $CURRENT_VER
  Каталог:     $VOXNODE_HOME
  Конфиг:      $VOXNODE_CONFIG_DIR/config.yaml
  Буфер (RAM): $VOXNODE_VAR_DIR/buffer  ($VOXNODE_TMPFS_SIZE)
  Буфер (SD):  $VOXNODE_VAR_DIR/spill

$(fmt_yellow 'Дальнейшие шаги:')

  1. Проверьте статус:
       voxnode doctor
       systemctl status voxnode-recorder

  2. Отредактируйте конфиг под свою точку:
       sudo nano $VOXNODE_CONFIG_DIR/config.yaml
     (device_id, device_secret, server_url, recorder.device для микрофона)

  3. Запись начнётся автоматически при подключённом микрофоне.
     Логи: journalctl -u voxnode-recorder -f

  4. Обновление в будущем:
       voxnode update

EOF
