"""
Touchpad dead-zone daemon for Dell XPS 15 9520 (or similar large touchpads).

Grabs the physical touchpad, creates a virtual clone via uinput, and
only forwards touch events that originate in the allowed central zone.

Also implements its own Disable-While-Typing: monitors the keyboard and
suppresses all touchpad events for a brief period after each keystroke.
This replaces libinput's DWT which can corrupt gesture state on virtual
devices.

Filtering rules (evaluated per SYN_REPORT batch):
  1. If typing was recent (within --dwt-timeout ms) -> suppress
  2. If ANY finger started in the active zone -> forward entire batch
  3. If ALL fingers are in dead zones -> suppress
  4. Once forwarded, keep forwarding until all fingers lift
"""

import argparse
import contextlib
import os
import select
import signal
import subprocess
import sys
import time

import evdev
from evdev import UInput, ecodes

# BTN_TOOL_* codes indexed by finger count (1-5)
_BTN_TOOL_BY_COUNT = [
    None,  # 0 fingers — not used
    ecodes.BTN_TOOL_FINGER,
    ecodes.BTN_TOOL_DOUBLETAP,
    ecodes.BTN_TOOL_TRIPLETAP,
    ecodes.BTN_TOOL_QUADTAP,
    ecodes.BTN_TOOL_QUINTTAP,
]
_BTN_TOOL_SET = set(_BTN_TOOL_BY_COUNT[1:])


def find_touchpad() -> str | None:
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if "touchpad" in dev.name.lower():
            return path
    return None


def find_keyboard() -> str | None:
    # Prefer the built-in AT keyboard over Bluetooth/USB keyboards
    candidates = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        caps = dev.capabilities()
        if ecodes.EV_KEY in caps and ecodes.EV_ABS not in caps:
            key_caps = caps[ecodes.EV_KEY]
            if any(k in key_caps for k in range(30, 56)):
                is_builtin = "isa0060" in (dev.phys or "") or "AT" in dev.name
                candidates.append((is_builtin, path, dev.name))
    candidates.sort(key=lambda x: (not x[0], x[1]))  # built-in first
    return candidates[0][1] if candidates else None


def create_virtual_device(real_dev: evdev.InputDevice) -> UInput:
    caps = real_dev.capabilities(absinfo=True)
    caps.pop(ecodes.EV_SYN, None)
    input_props = list(real_dev.input_props()) if hasattr(real_dev, "input_props") else []
    return UInput(
        events=caps,
        name=f"{real_dev.name} (zone-filtered)",
        vendor=real_dev.info.vendor,
        product=real_dev.info.product,
        version=real_dev.info.version,
        bustype=real_dev.info.bustype,
        input_props=input_props,
    )


def xinput_set_prop(device_name: str, prop: str, value: str) -> None:
    try:
        xid = subprocess.check_output(
            ["xinput", "list", "--id-only", device_name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        subprocess.run(
            ["xinput", "set-prop", xid, prop, value],
            check=True,
            capture_output=True,
        )
        print(f"Set {prop}={value} on {device_name} (xinput id {xid})")
    except Exception as e:
        print(f"Warning: could not set {prop}: {e}")


def xinput_disable(device_name: str) -> None:
    try:
        xid = subprocess.check_output(
            ["xinput", "list", "--id-only", device_name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        subprocess.run(
            ["xinput", "disable", xid],
            check=True,
            capture_output=True,
        )
        print(f"Disabled {device_name} in X (xinput id {xid})")
    except Exception as e:
        print(f"Warning: could not disable {device_name}: {e}")


def xinput_toggle(device_name: str) -> None:
    """Disable then re-enable a device in X to force re-read."""
    try:
        xid = subprocess.check_output(
            ["xinput", "list", "--id-only", device_name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        subprocess.run(["xinput", "disable", xid], check=True, capture_output=True)
        time.sleep(0.3)
        subprocess.run(["xinput", "enable", xid], check=True, capture_output=True)
        print(f"Toggled {device_name} in X (xinput id {xid})")
    except Exception as e:
        print(f"Warning: could not toggle {device_name}: {e}")


# Modifier and function keys that should NOT trigger DWT
_NON_TYPING_KEYS = {
    ecodes.KEY_LEFTCTRL,
    ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_LEFTALT,
    ecodes.KEY_RIGHTALT,
    ecodes.KEY_LEFTSHIFT,
    ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTMETA,
    ecodes.KEY_RIGHTMETA,
    ecodes.KEY_CAPSLOCK,
    ecodes.KEY_NUMLOCK,
    ecodes.KEY_SCROLLLOCK,
    ecodes.KEY_FN,
    # Function keys
    ecodes.KEY_F1,
    ecodes.KEY_F2,
    ecodes.KEY_F3,
    ecodes.KEY_F4,
    ecodes.KEY_F5,
    ecodes.KEY_F6,
    ecodes.KEY_F7,
    ecodes.KEY_F8,
    ecodes.KEY_F9,
    ecodes.KEY_F10,
    ecodes.KEY_F11,
    ecodes.KEY_F12,
    # Navigation (allow touchpad during these)
    ecodes.KEY_UP,
    ecodes.KEY_DOWN,
    ecodes.KEY_LEFT,
    ecodes.KEY_RIGHT,
    ecodes.KEY_PAGEUP,
    ecodes.KEY_PAGEDOWN,
    ecodes.KEY_HOME,
    ecodes.KEY_END,
    ecodes.KEY_INSERT,
    ecodes.KEY_DELETE,
    # Media/special
    ecodes.KEY_VOLUMEUP,
    ecodes.KEY_VOLUMEDOWN,
    ecodes.KEY_MUTE,
    ecodes.KEY_BRIGHTNESSUP,
    ecodes.KEY_BRIGHTNESSDOWN,
    ecodes.KEY_PRINT,
    ecodes.KEY_PAUSE,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Touchpad dead-zone daemon with built-in DWT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--left", type=float, default=20, help="Left dead zone %%")
    parser.add_argument("--right", type=float, default=20, help="Right dead zone %%")
    parser.add_argument("--top", type=float, default=0, help="Top dead zone %%")
    parser.add_argument("--bottom", type=float, default=0, help="Bottom dead zone %%")
    parser.add_argument(
        "--dwt-timeout",
        type=float,
        default=0.5,
        help="Seconds to suppress touchpad after last keystroke (default: 0.5)",
    )
    parser.add_argument("--device", type=str, default=None, help="Touchpad event device")
    parser.add_argument("--keyboard", type=str, default=None, help="Keyboard event device")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Find devices
    dev_path = args.device or find_touchpad()
    if not dev_path:
        print("Error: No touchpad found.", file=sys.stderr)
        sys.exit(1)
    dev = evdev.InputDevice(dev_path)
    print(f"Touchpad: {dev.name} ({dev_path})")

    kb_path = args.keyboard or find_keyboard()
    if not kb_path:
        print("Warning: No keyboard found, DWT disabled.", file=sys.stderr)
        kb = None
    else:
        kb = evdev.InputDevice(kb_path)
        print(f"Keyboard: {kb.name} ({kb_path})")

    # Coordinate ranges
    abs_caps = dict(dev.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
    x_info = abs_caps.get(ecodes.ABS_MT_POSITION_X) or abs_caps.get(ecodes.ABS_X)
    y_info = abs_caps.get(ecodes.ABS_MT_POSITION_Y) or abs_caps.get(ecodes.ABS_Y)
    if not x_info or not y_info:
        print("Error: Cannot determine coordinate range.", file=sys.stderr)
        sys.exit(1)

    x_min, x_max = x_info.min, x_info.max
    y_min, y_max = y_info.min, y_info.max
    x_range, y_range = x_max - x_min, y_max - y_min

    dead_left = x_min + int(x_range * args.left / 100)
    dead_right = x_max - int(x_range * args.right / 100)
    dead_top = y_min + int(y_range * args.top / 100)
    dead_bottom = y_max - int(y_range * args.bottom / 100)

    print(f"Active zone: X [{dead_left}, {dead_right}], Y [{dead_top}, {dead_bottom}]")
    print(f"Dead zones: L={args.left}% R={args.right}% T={args.top}% B={args.bottom}%")
    print(f"DWT timeout: {args.dwt_timeout}s")

    # Create virtual device and grab real one
    virt = create_virtual_device(dev)
    print(f"Virtual device: {virt.name} ({virt.device.path})")
    for attempt in range(10):
        try:
            dev.grab()
            break
        except OSError as e:
            if e.errno == 16 and attempt < 9:  # EBUSY
                print(f"Device busy, retrying ({attempt + 1}/10)...")
                time.sleep(1)
            else:
                raise
    print("Grabbed real device.")

    time.sleep(0.5)
    xinput_disable(dev.name)
    xinput_set_prop(virt.name, "libinput Disable While Typing Enabled", "0")
    print("Running... (Ctrl+C or SIGTERM to stop)")

    def in_active_zone(x: int, y: int) -> bool:
        return dead_left <= x <= dead_right and dead_top <= y <= dead_bottom

    def rewrite_btn_tool(
        batch: list[evdev.InputEvent], finger_count: int
    ) -> list[evdev.InputEvent]:
        """Rewrite BTN_TOOL_* events so the reported finger count matches finger_count."""
        # Grab timestamp from any event in the batch for the injected events
        ts_sec = batch[0].sec if batch else 0
        ts_usec = batch[0].usec if batch else 0
        out: list[evdev.InputEvent] = []
        for ev in batch:
            if ev.type == ecodes.EV_KEY and ev.code in _BTN_TOOL_SET:
                continue  # strip original BTN_TOOL_* events
            out.append(ev)
        # Always inject all BTN_TOOL_* with correct state
        clamped = max(0, min(finger_count, 5))
        for i in range(1, 6):
            val = 1 if i == clamped else 0
            out.append(evdev.InputEvent(ts_sec, ts_usec, ecodes.EV_KEY, _BTN_TOOL_BY_COUNT[i], val))
        return out

    # Touchpad state
    slot_pos: dict[int, tuple[int, int]] = {}
    slot_dead: dict[int, bool | None] = {}
    slot_tid: dict[int, int] = {}  # slot -> real tracking ID
    current_slot = 0
    touch_forwarded = False
    batch: list[evdev.InputEvent] = []

    # DWT state
    last_key_time = 0.0

    # Idle-wake: toggle xinput after gaps (lock screen, suspend)
    # CLOCK_BOOTTIME counts suspend time unlike monotonic
    last_touch_time = 0.0
    IDLE_THRESHOLD = 30.0  # seconds

    def is_typing() -> bool:
        return (time.monotonic() - last_key_time) < args.dwt_timeout

    def reset_all() -> None:
        nonlocal current_slot, touch_forwarded
        slot_pos.clear()
        slot_dead.clear()
        slot_tid.clear()
        current_slot = 0
        touch_forwarded = False

    def synthetic_lift() -> None:
        """Send synthetic lift to virtual device."""
        for slot in range(5):
            virt.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
            virt.write(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1)
        virt.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 0)
        for code in _BTN_TOOL_SET:
            virt.write(ecodes.EV_KEY, code, 0)
        virt.syn()

    def cleanup(*_args: object) -> None:
        with contextlib.suppress(Exception):
            dev.ungrab()
        with contextlib.suppress(Exception):
            virt.close()
        print("\nClean shutdown.")
        sys.exit(0)

    def toggle_virt(*_args: object) -> None:
        xinput_toggle(virt.name)
        print("SIGUSR1: toggled virtual device")

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGUSR1, toggle_virt)

    pid_file = os.path.expanduser("~/.touchpad-zones.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    # Build device map for select loop
    devices = {dev.fd: dev}
    if kb:
        devices[kb.fd] = kb

    try:
        while True:
            # Use select with a short timeout so DWT state transitions
            # aren't delayed. Timeout = remaining DWT time or 1 second.
            r, _, _ = select.select(devices.keys(), [], [], 1.0)

            for fd in r:
                source = devices[fd]

                # --- Keyboard events ---
                if source is kb:
                    for event in source.read():
                        if (
                            event.type == ecodes.EV_KEY
                            and event.value == 1  # key down only
                            and event.code not in _NON_TYPING_KEYS
                        ):
                            was_typing = is_typing()
                            last_key_time = time.monotonic()
                            if not was_typing and args.verbose:
                                print("  DWT: typing started")
                            # If virtual device has active touches, send
                            # synthetic lift so gestures are cleanly cancelled
                            if touch_forwarded:
                                synthetic_lift()
                                reset_all()
                                batch.clear()
                                if args.verbose:
                                    print("  DWT: synthetic lift sent")
                    continue

                # --- Touchpad events ---
                for event in source.read():
                    # SYN_DROPPED
                    if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                        if touch_forwarded:
                            synthetic_lift()
                        reset_all()
                        batch.clear()
                        if args.verbose:
                            print("  SYN_DROPPED: reset + synthetic lift")
                        continue

                    # Track slot
                    if event.type == ecodes.EV_ABS and event.code == ecodes.ABS_MT_SLOT:
                        current_slot = event.value

                    # BTN_TOUCH=0
                    if (
                        event.type == ecodes.EV_KEY
                        and event.code == ecodes.BTN_TOUCH
                        and event.value == 0
                    ):
                        slot_pos.clear()
                        slot_dead.clear()
                        slot_tid.clear()
                        current_slot = 0

                    # Track positions and finger up/down
                    if event.type == ecodes.EV_ABS:
                        if event.code == ecodes.ABS_MT_POSITION_X:
                            old = slot_pos.get(current_slot, (0, 0))
                            slot_pos[current_slot] = (event.value, old[1])
                        elif event.code == ecodes.ABS_MT_POSITION_Y:
                            old = slot_pos.get(current_slot, (0, 0))
                            slot_pos[current_slot] = (old[0], event.value)
                        elif event.code == ecodes.ABS_MT_TRACKING_ID:
                            if event.value >= 0:
                                slot_dead[current_slot] = None
                                slot_tid[current_slot] = event.value
                            else:
                                slot_dead.pop(current_slot, None)
                                slot_pos.pop(current_slot, None)
                                slot_tid.pop(current_slot, None)

                    # Process at SYN_REPORT
                    if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                        # Resolve pending slots
                        for slot in list(slot_dead):
                            if slot_dead[slot] is None:
                                pos = slot_pos.get(slot, (0, 0))
                                slot_dead[slot] = not in_active_zone(pos[0], pos[1])
                                if args.verbose:
                                    tag = "BLOCKED" if slot_dead[slot] else "ACTIVE"
                                    print(f"  {tag} slot {slot} at ({pos[0]}, {pos[1]})")

                        any_active = any(not d for d in slot_dead.values())
                        no_slots = len(slot_dead) == 0

                        # Idle-wake: xinput toggle to force X to re-read
                        # the virtual device after lock / suspend.
                        # Synthetic lift cleans up stale virtual-device state
                        # but we do NOT skip this batch — let the touch land.
                        now = time.clock_gettime(time.CLOCK_BOOTTIME)
                        idle_gap = now - last_touch_time if last_touch_time else 0
                        if last_touch_time and idle_gap > IDLE_THRESHOLD:
                            if touch_forwarded:
                                synthetic_lift()
                                touch_forwarded = False
                            xinput_toggle(virt.name)
                            if args.verbose:
                                print(f"  WAKE: toggled after {idle_gap:.0f}s idle")
                        last_touch_time = now

                        # DWT: suppress everything during typing
                        if is_typing() and not touch_forwarded:
                            if args.verbose and any_active:
                                print("  [DWT suppressed]")
                            batch = []
                            # Clear slot state so touches that started during
                            # DWT cannot leak through when DWT expires without
                            # their tracking IDs (which were in suppressed
                            # batches). Forces a clean re-land after typing.
                            slot_pos.clear()
                            slot_dead.clear()
                            slot_tid.clear()
                            current_slot = 0
                            continue

                        total_count = len(slot_dead)

                        if any_active:
                            if not touch_forwarded:
                                # Inject complete MT state for all tracked
                                # fingers so that fingers whose tracking IDs
                                # were in previously-suppressed batches become
                                # visible to libinput on the virtual device.
                                for slot in sorted(slot_tid):
                                    virt.write(ecodes.EV_ABS, ecodes.ABS_MT_SLOT, slot)
                                    virt.write(
                                        ecodes.EV_ABS,
                                        ecodes.ABS_MT_TRACKING_ID,
                                        slot_tid[slot],
                                    )
                                    if slot in slot_pos:
                                        virt.write(
                                            ecodes.EV_ABS,
                                            ecodes.ABS_MT_POSITION_X,
                                            slot_pos[slot][0],
                                        )
                                        virt.write(
                                            ecodes.EV_ABS,
                                            ecodes.ABS_MT_POSITION_Y,
                                            slot_pos[slot][1],
                                        )
                                virt.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, 1)
                                clamped = max(0, min(total_count, 5))
                                for i in range(1, 6):
                                    virt.write(
                                        ecodes.EV_KEY,
                                        _BTN_TOOL_BY_COUNT[i],
                                        1 if i == clamped else 0,
                                    )
                                virt.syn()
                                touch_forwarded = True
                                if args.verbose:
                                    print(
                                        f"  >> FORWARD START: {len(batch)} events, "
                                        f"{total_count} fingers (state injected)"
                                    )
                            for ev in batch:
                                virt.write_event(ev)
                            virt.syn()
                        elif touch_forwarded:
                            for ev in batch:
                                virt.write_event(ev)
                            virt.syn()
                            if no_slots:
                                touch_forwarded = False
                                if args.verbose:
                                    print("  >> FORWARD END: all fingers lifted")
                        elif not no_slots:
                            if args.verbose:
                                positions = {s: slot_pos.get(s, (0, 0)) for s in slot_dead}
                                print(f"  [suppressed] {total_count} fingers: {positions}")

                        batch = []
                    else:
                        batch.append(event)

    except OSError as e:
        if e.errno == 19:
            print(f"Device removed: {e}", file=sys.stderr)
        else:
            raise
    finally:
        cleanup()


if __name__ == "__main__":
    main()
