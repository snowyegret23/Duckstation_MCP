from __future__ import annotations

import atexit
import importlib
import importlib.util
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any


class BackgroundInputError(RuntimeError):
    pass


_BUTTON_ATTRIBUTES = {
    "up": "XUSB_GAMEPAD_DPAD_UP",
    "down": "XUSB_GAMEPAD_DPAD_DOWN",
    "left": "XUSB_GAMEPAD_DPAD_LEFT",
    "right": "XUSB_GAMEPAD_DPAD_RIGHT",
    "start": "XUSB_GAMEPAD_START",
    "select": "XUSB_GAMEPAD_BACK",
    "l3": "XUSB_GAMEPAD_LEFT_THUMB",
    "r3": "XUSB_GAMEPAD_RIGHT_THUMB",
    "l1": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "r1": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "cross": "XUSB_GAMEPAD_A",
    "circle": "XUSB_GAMEPAD_B",
    "square": "XUSB_GAMEPAD_X",
    "triangle": "XUSB_GAMEPAD_Y",
}
_TRIGGERS = {"l2": "left_trigger_float", "r2": "right_trigger_float"}


def normalize_button(button: str) -> str:
    normalized = button.strip().lower().replace("-", "").replace("_", "")
    aliases = {"a": "cross", "b": "circle", "x": "square", "y": "triangle", "back": "select"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in _BUTTON_ATTRIBUTES and normalized not in _TRIGGERS:
        valid = ", ".join(sorted((*_BUTTON_ATTRIBUTES, *_TRIGGERS)))
        raise ValueError(f"unknown button {button!r}; valid: {valid}")
    return normalized


class BackgroundGamepad:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vg: Any = None
        self._pad: Any = None
        self._buttons: dict[str, bool] = {name: False for name in _BUTTON_ATTRIBUTES}
        self._triggers: dict[str, float] = {name: 0.0 for name in _TRIGGERS}
        self._sticks = {"left": (0.0, 0.0), "right": (0.0, 0.0)}

    def ensure(self) -> None:
        if self._pad is not None:
            return
        try:
            self._vg = importlib.import_module("vgamepad")
            self._pad = self._vg.VX360Gamepad()
        except Exception as exc:
            self._vg = None
            self._pad = None
            raise BackgroundInputError(
                "could not create the ViGEm XInput controller; install this project and verify ViGEmBus"
            ) from exc

    def _set_button(self, name: str, pressed: bool) -> None:
        if name in _TRIGGERS:
            value = 1.0 if pressed else 0.0
            getattr(self._pad, _TRIGGERS[name])(value_float=value)
            self._triggers[name] = value
            return
        button = getattr(self._vg.XUSB_BUTTON, _BUTTON_ATTRIBUTES[name])
        if pressed:
            self._pad.press_button(button=button)
        else:
            self._pad.release_button(button=button)
        self._buttons[name] = pressed

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backend": "ViGEm/XInput",
                "connected": self._pad is not None,
                "foreground_activation": False,
                "buttons": {**self._buttons, **{name: value > 0.0 for name, value in self._triggers.items()}},
                "sticks": dict(self._sticks),
            }

    def press(self, button: str, duration_ms: int = 100) -> dict[str, Any]:
        name = normalize_button(button)
        if not 1 <= duration_ms <= 10_000:
            raise ValueError("duration_ms must be between 1 and 10000")
        with self._lock:
            self.ensure()
            previous = self._triggers[name] > 0.0 if name in _TRIGGERS else self._buttons[name]
            self._set_button(name, True)
            self._pad.update()
            try:
                time.sleep(duration_ms / 1000.0)
            finally:
                self._set_button(name, previous)
                self._pad.update()
            return {"button": name, "duration_ms": duration_ms, **self.status()}

    def set_buttons(self, buttons: dict[str, bool | None]) -> dict[str, Any]:
        normalized: list[tuple[str, bool]] = []
        for button, pressed in buttons.items():
            name = normalize_button(button)
            if pressed is None:
                continue
            if not isinstance(pressed, bool):
                raise ValueError(f"button state for {button!r} must be true, false, or null")
            normalized.append((name, pressed))
        with self._lock:
            self.ensure()
            for name, pressed in normalized:
                self._set_button(name, pressed)
            self._pad.update()
            return self.status()

    def set_analog(self, x: float, y: float, stick: str = "left") -> dict[str, Any]:
        side = stick.strip().lower()
        if side not in self._sticks:
            raise ValueError("stick must be 'left' or 'right'")
        if not -1.0 <= x <= 1.0 or not -1.0 <= y <= 1.0:
            raise ValueError("x and y must be between -1.0 and 1.0")
        with self._lock:
            self.ensure()
            getattr(self._pad, f"{side}_joystick_float")(x_value_float=x, y_value_float=y)
            self._sticks[side] = (x, y)
            self._pad.update()
            return self.status()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self.ensure()
            self._pad.reset()
            self._pad.update()
            self._buttons = {name: False for name in _BUTTON_ATTRIBUTES}
            self._triggers = {name: 0.0 for name in _TRIGGERS}
            self._sticks = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
            return self.status()

    def close(self) -> None:
        with self._lock:
            if self._pad is None:
                return
            try:
                self._pad.reset()
                self._pad.update()
            except Exception:
                pass
            self._pad = None
            self._vg = None


gamepad = BackgroundGamepad()
atexit.register(gamepad.close)


def xinput_module_available() -> bool:
    return importlib.util.find_spec("vgamepad") is not None


def choose_backend(backend: str) -> tuple[str, str | None]:
    requested = backend.strip().lower()
    if requested not in {"auto", "keyboard", "xinput"}:
        raise ValueError("backend must be 'auto', 'keyboard', or 'xinput'")
    if requested == "keyboard":
        return "keyboard", None
    try:
        gamepad.ensure()
        return "xinput", None
    except BackgroundInputError as exc:
        if requested == "xinput":
            raise
        return "keyboard", str(exc)


def default_config_candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    return [local / "DuckStation" / "settings.ini", Path.home() / "Documents" / "DuckStation" / "settings.ini"]


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        if start is not None:
            return start, index
        if stripped[1:-1] == section:
            start = index
    return (start, len(lines)) if start is not None else None


def _update_ini(
    path: Path,
    scalar_values: dict[str, dict[str, str]],
    list_values: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    raw = path.read_bytes() if path.exists() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = text.splitlines()
    changed = False
    added_bindings = 0

    for section in dict.fromkeys((*scalar_values, *list_values)):
        bounds = _section_bounds(lines, section)
        if bounds is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            bounds = (len(lines) - 1, len(lines))
            changed = True
        start, end = bounds
        body = lines[start + 1 : end]

        for key, value in scalar_values.get(section, {}).items():
            indices = [
                index
                for index, line in enumerate(body)
                if "=" in line and line.split("=", 1)[0].strip() == key
            ]
            replacement = f"{key} = {value}"
            if indices:
                if body[indices[0]] != replacement:
                    body[indices[0]] = replacement
                    changed = True
            else:
                body.append(replacement)
                changed = True

        for key, values in list_values.get(section, {}).items():
            existing = {
                line.split("=", 1)[1].strip()
                for line in body
                if "=" in line and line.split("=", 1)[0].strip() == key
            }
            for value in values:
                if value in existing:
                    continue
                body.append(f"{key} = {value}")
                existing.add(value)
                added_bindings += 1
                changed = True

        lines[start + 1 : end] = body

    if changed:
        payload = newline.join(lines) + newline
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + payload.encode("utf-8"))
    return {"changed": changed, "added_bindings": added_bindings, "newline": "CRLF" if newline == "\r\n" else "LF"}


def configure_background_input(config_path: str | None = None, device_index: int = 0) -> dict[str, Any]:
    if not 0 <= device_index <= 3:
        raise ValueError("device_index must be between 0 and 3")
    path = Path(config_path) if config_path else next(
        (candidate for candidate in default_config_candidates() if candidate.exists()),
        default_config_candidates()[0],
    )
    device = f"XInput-{device_index}"
    bindings = {
        "Up": ["Keyboard/UpArrow", f"{device}/DPadUp"],
        "Right": ["Keyboard/RightArrow", f"{device}/DPadRight"],
        "Down": ["Keyboard/DownArrow", f"{device}/DPadDown"],
        "Left": ["Keyboard/LeftArrow", f"{device}/DPadLeft"],
        "Triangle": ["Keyboard/I", f"{device}/Y"],
        "Circle": ["Keyboard/L", f"{device}/B"],
        "Cross": ["Keyboard/K", f"{device}/A"],
        "Square": ["Keyboard/J", f"{device}/X"],
        "Select": ["Keyboard/Backspace", f"{device}/Back"],
        "Start": ["Keyboard/Enter", f"{device}/Start"],
        "L1": ["Keyboard/Q", f"{device}/LeftShoulder"],
        "L2": ["Keyboard/1", f"{device}/+LeftTrigger"],
        "R1": ["Keyboard/E", f"{device}/RightShoulder"],
        "R2": ["Keyboard/3", f"{device}/+RightTrigger"],
        "L3": ["Keyboard/2", f"{device}/LeftStick"],
        "R3": ["Keyboard/4", f"{device}/RightStick"],
        "LUp": ["Keyboard/W", f"{device}/-LeftY"],
        "LRight": ["Keyboard/D", f"{device}/+LeftX"],
        "LDown": ["Keyboard/S", f"{device}/+LeftY"],
        "LLeft": ["Keyboard/A", f"{device}/-LeftX"],
        "RUp": ["Keyboard/T", f"{device}/-RightY"],
        "RRight": ["Keyboard/H", f"{device}/+RightX"],
        "RDown": ["Keyboard/G", f"{device}/+RightY"],
        "RLeft": ["Keyboard/F", f"{device}/-RightX"],
    }
    backup = path.with_name(f"{path.name}.background-mcp.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    result = _update_ini(
        path,
        {"Main": {"DisableBackgroundInput": "false"}, "InputSources": {"XInput": "true"}},
        {"Pad1": bindings},
    )
    return {
        **result,
        "path": str(path),
        "backup_path": str(backup) if backup.exists() else None,
        "device": device,
        "keyboard_fallback": True,
        "preserved_existing_bindings": True,
        "restart_required": result["changed"],
        "foreground_activation": False,
    }
