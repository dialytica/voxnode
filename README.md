# voxnode

**Mass-deployable audio recording agent for Raspberry Pi. Records in segments, buffers offline, uploads when online.**

Part of the [dialytica](https://github.com/dialytica) project. Designed to run on a
Raspberry Pi 3 with a [Seeed ReSpeaker XVF3800](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/)
USB microphone array, capturing in-store conversations in 1-minute Opus segments,
buffering to RAM when the network is down, and uploading to the dialytica server
when connectivity returns.

## Features

- 🎙️ **24/7 recording** of 1-minute Opus segments (16 kHz mono, ~259 MB/day)
- 💾 **RAM buffering** (tmpfs) to avoid wearing out the SD card; spills to disk only when full
- 📶 **Captive portal** for first-boot WiFi setup — no SSH/keyboard needed, configure from a phone
- 🔄 **Auto-update** (oh-myzsh-style) via systemd timer + git pull --rebase
- 🔁 **Store-and-forward** upload: never loses a recording, retries with exponential backoff
- 📦 **One-line install** for rapid rollout across many devices
- 🔐 **HMAC-signed** uploads to prevent spoofing

## Install

```sh
sh -c "$(curl -fsSL https://raw.githubusercontent.com/dialytica/voxnode/main/tools/install.sh)"
```

After install, reboot. If no WiFi is configured, the Pi broadcasts an open hotspot
named `voxnode-setup-XXXX`. Join it from a phone and follow the captive portal.

## Requirements (target device)

- Raspberry Pi 3 (or newer) running Raspberry Pi OS Lite (Debian 12/13)
- Seeed ReSpeaker XVF3800 USB microphone array
- ~5 W power supply (2.5 A recommended)
- A microSD card (16 GB Class 10 or larger)

## Repository layout

```
tools/      One-line installer + auto-update (ohmyzsh-style)
voxnode/    Core Python package (recorder, uploader, portal, watchdog)
systemd/    Service units
config/     Example config
scripts/    Operational scripts (reset-config, test-audio)
templates/  Captive portal HTML
```

## License

Proprietary — © dialytica. See [`LICENSE`](LICENSE) for details.
