"""voxnode CLI — точка входа `python -m voxnode` и симлинк `voxnode`.

Команды (аналог `omz` у ohmyzsh):
    voxnode version    — показать версию + последний апдейт
    voxnode update     — проверить и применить обновление вручную
    voxnode changelog  — коммиты с последнего обновления
    voxnode doctor     — диагностика (микрофон, сеть, буфер, диск)

Устанавливается через install.sh как /usr/local/bin/voxnode.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from voxnode.version import get_last_version, get_version, get_version_detail


def cmd_version(_args) -> int:
    """Показать текущую версию (тег + SHA) и последний апдейт."""
    detail = get_version_detail()
    print(f"voxnode {detail['display']}")
    if detail["tag"]:
        print(f"  тег:   {detail['tag']}")
    print(f"  SHA:   {detail['sha']}")
    last = get_last_version()
    if last:
        print(f"Последнее обновление было с: {last}")
    else:
        print("Обновлений ещё не было.")
    return 0


def cmd_update(args) -> int:
    """Запустить проверку/применение обновления вручную.

    По умолчанию запускает check_for_upgrade.sh (как timer): сравнивает тег
    с GitHub releases/latest и обновляется только если есть новый релиз.

    С --tag vX.Y.Z принудительно переключается на указанный тег.
    С --check только проверяет наличие обновления, не применяя.
    """
    home = Path(__file__).resolve().parent.parent

    if args.check:
        check = home / "tools" / "check_for_upgrade.sh"
        if not check.is_file():
            print(f"✗ check_for_upgrade.sh не найден: {check}", file=sys.stderr)
            return 1
        return subprocess.call(["sh", str(check)])

    upgrade = home / "tools" / "upgrade.sh"
    if not upgrade.is_file():
        print(f"✗ upgrade.sh не найден: {upgrade}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    if args.tag:
        print(f"→ Принудительное обновление на тег {args.tag}")
        env["VOXNODE_TARGET_TAG"] = args.tag
    else:
        print("→ Проверяю и применяю последний релиз (через check_for_upgrade.sh)")
        check = home / "tools" / "check_for_upgrade.sh"
        if check.is_file():
            return subprocess.call(["sh", str(check)])
    return subprocess.call(["sh", str(upgrade)], env=env)


def cmd_changelog(_args) -> int:
    """Показать коммиты с последнего обновления."""
    home = Path(__file__).resolve().parent.parent
    last = get_last_version()
    if not last:
        print("Обновлений ещё не было — changelog недоступен.")
        return 0
    print(f"Коммиты с {last[:7]} до {get_version()[:7]}:\n")
    try:
        out = subprocess.check_output(
            ["git", "-C", str(home), "log", "--oneline", f"{last}..HEAD"],
            text=True,
            timeout=5,
        )
        print(out or "(нет новых коммитов)")
    except subprocess.SubprocessError as e:
        print(f"✗ git log не удался: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(_args) -> int:
    """Диагностика состояния voxnode."""
    print(" voxnode doctor — диагностика\n")

    issues = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal issues
        mark = "✓" if ok else "✗"
        if not ok:
            issues += 1
        line = f"  {mark} {label}"
        if detail:
            line += f": {detail}"
        print(line)

    # --- Версия ---
    check("Версия voxnode", True, get_version())

    # --- ffmpeg ---
    ffmpeg = shutil.which("ffmpeg")
    check("ffmpeg установлен", ffmpeg is not None, ffmpeg or "не найден в PATH")

    # --- Python venv ---
    venv = Path("/opt/voxnode/.venv/bin/python")
    check("venv", venv.is_file(), str(venv) if venv.is_file() else "не найден")

    # --- Конфиг ---
    cfg = Path("/etc/voxnode/config.yaml")
    check("Конфиг /etc/voxnode/config.yaml", cfg.is_file(),
          "есть" if cfg.is_file() else "ОТСУТСТВУЕТ")

    # --- Буфер RAM ---
    ram = Path("/var/voxnode/buffer")
    check("RAM-буфер /var/voxnode/buffer", ram.is_dir(),
          "есть" if ram.is_dir() else "нет")

    # Является ли RAM-буфер tmpfs? (парсим /proc/mounts)
    if ram.is_dir():
        try:
            mounts = Path("/proc/mounts").read_text()
            mountpoint_str = str(ram)
            is_tmpfs = any(
                line.startswith("tmpfs ") and line.split()[1] == mountpoint_str
                for line in mounts.splitlines()
            )
            check("RAM-буфер примонтирован как tmpfs", is_tmpfs)
        except Exception:
            pass

    # --- Микрофон (arecord) ---
    try:
        out = subprocess.check_output(["arecord", "-l"], text=True, timeout=3)
        cards = [l for l in out.splitlines() if "card" in l.lower()]
        check("USB-микрофон обнаружен", bool(cards),
              f"{len(cards)} карта(ы)" if cards else "НЕТ капчур-устройств")
    except (subprocess.SubprocessError, FileNotFoundError):
        check("arecord доступен", False, "не вызывается")

    # --- Сервисы systemd ---
    for svc in ("voxnode-recorder", "voxnode-uploader", "voxnode-watchdog"):
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", svc], text=True, timeout=3
            ).strip()
            check(f"Сервис {svc}", out == "active", out)
        except subprocess.SubprocessError:
            check(f"Сервис {svc}", False, "не удалось проверить")

    # --- Сеть ---
    try:
        out = subprocess.check_output(
            ["ip", "-br", "route"], text=True, timeout=3
        )
        has_default = "default" in out
        check("Маршрут по умолчанию (интернет)", has_default)
    except subprocess.SubprocessError:
        pass

    # --- Spill-буфер (SD) + свободное место ---
    spill = Path("/var/voxnode/spill")
    if spill.is_dir():
        import shutil as _shutil
        total, used, free = _shutil.disk_usage(spill)
        free_pct = free / total * 100 if total else 0
        check("Свободно на SD под spill", free_pct > 5, f"{free_pct:.0f}%")

    # --- Файлы в буфере (для диагностики) ---
    ram = Path("/var/voxnode/buffer")
    if ram.is_dir():
        files = list(ram.iterdir())
        check("Файлов в RAM-буфере", True, f"{len(files)}")

    print()
    if issues == 0:
        print(" Все проверки пройдены.\n")
        return 0
    print(f" {issues} проблем(а) найдено. См. выше.\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="voxnode",
        description="voxnode — mass-deployable audio recording agent",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="показать версию (тег + SHA)")
    p_update = sub.add_parser("update", help="проверить и применить обновление")
    p_update.add_argument("--tag", help="принудительно переключиться на указанный тег (например v0.2.0)")
    p_update.add_argument("--check", action="store_true", help="только проверить, не применяя")
    sub.add_parser("changelog", help="коммиты с последнего обновления")
    sub.add_parser("doctor", help="диагностика состояния")

    args = parser.parse_args(argv)

    if args.cmd == "version":
        return cmd_version(args)
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "changelog":
        return cmd_changelog(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
