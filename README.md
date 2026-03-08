# touchpad-zones

Touchpad dead-zone daemon for Dell XPS 15 9520 (or similar large touchpads). Grabs the physical touchpad via evdev, creates a virtual clone via uinput, and only forwards touch events that originate in the allowed central zone.

Includes built-in Disable-While-Typing (DWT) that replaces libinput's DWT, which can corrupt gesture state on virtual devices.

## Features

- Configurable dead zones (left, right, top, bottom as percentages)
- Built-in DWT with configurable timeout
- Auto-detects touchpad and keyboard devices
- Runs as a systemd user service
- X11 only (uses xinput to disable libinput DWT on the virtual device)

## Prerequisites

- Linux with evdev/uinput support
- Python 3.11+ (managed via pyenv)
- [uv](https://github.com/astral-sh/uv) package manager
- X11 (for xinput DWT disable)
- User must have access to `/dev/uinput` (typically via `input` group)

## Quick Start

```bash
make setup
```

### Run directly

```bash
uv run touchpad-zones --left 15 --right 15 --verbose
```

### Install as systemd user service

```bash
make service-install
systemctl --user start touchpad-zones
```

### Service management

```bash
make service-status     # Show service status
make service-restart    # Restart the service
make service-log        # Show full service log
make service-log-tail   # Follow service log (tail -f)
```

## Usage

```
touchpad-zones [--left %] [--right %] [--top %] [--bottom %]
               [--dwt-timeout SECONDS] [--device PATH] [--keyboard PATH]
               [--verbose]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--left` | 15 | Left dead zone percentage |
| `--right` | 15 | Right dead zone percentage |
| `--top` | 0 | Top dead zone percentage |
| `--bottom` | 0 | Bottom dead zone percentage |
| `--dwt-timeout` | 0.5 | Seconds to suppress touchpad after typing |
| `--device` | auto | Touchpad event device path |
| `--keyboard` | auto | Keyboard event device path |
| `--verbose` | off | Print zone filtering decisions |

## Development

```bash
make help             # Show all available commands
make lint             # Run linter
make format           # Format code
make check            # Run all checks (lint + format-check)
```

## License

MIT
