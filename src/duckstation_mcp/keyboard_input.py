from __future__ import annotations

import ctypes
import threading
import time
from pathlib import Path
from typing import Any

from .background_input import normalize_button


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostMessageW.restype = ctypes.c_bool
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
user32.VkKeyScanW.restype = ctypes.c_short

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MAPVK_VK_TO_VSC = 0
_EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
_NAMED_KEYS = {
    "Backspace": 0x08,
    "Tab": 0x09,
    "Return": 0x0D,
    "Enter": 0x0D,
    "Shift": 0x10,
    "Control": 0x11,
    "Alt": 0x12,
    "Escape": 0x1B,
    "Space": 0x20,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "Left": 0x25,
    "LeftArrow": 0x25,
    "Up": 0x26,
    "UpArrow": 0x26,
    "Right": 0x27,
    "RightArrow": 0x27,
    "Down": 0x28,
    "DownArrow": 0x28,
    "Insert": 0x2D,
    "Delete": 0x2E,
}
_BUTTON_KEYS = {
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "left": "Left",
    "triangle": "Triangle",
    "circle": "Circle",
    "cross": "Cross",
    "square": "Square",
    "select": "Select",
    "start": "Start",
    "l1": "L1",
    "l2": "L2",
    "r1": "R1",
    "r2": "R2",
    "l3": "L3",
    "r3": "R3",
}
_DEFAULT_BINDINGS = {
    "Up": "Keyboard/UpArrow",
    "Right": "Keyboard/RightArrow",
    "Down": "Keyboard/DownArrow",
    "Left": "Keyboard/LeftArrow",
    "Triangle": "Keyboard/I",
    "Circle": "Keyboard/L",
    "Cross": "Keyboard/K",
    "Square": "Keyboard/J",
    "Select": "Keyboard/Backspace",
    "Start": "Keyboard/Enter",
    "L1": "Keyboard/Q",
    "L2": "Keyboard/1",
    "R1": "Keyboard/E",
    "R2": "Keyboard/3",
    "L3": "Keyboard/2",
    "R3": "Keyboard/4",
    "LUp": "Keyboard/W",
    "LRight": "Keyboard/D",
    "LDown": "Keyboard/S",
    "LLeft": "Keyboard/A",
    "RUp": "Keyboard/T",
    "RRight": "Keyboard/H",
    "RDown": "Keyboard/G",
    "RLeft": "Keyboard/F",
}


def _read_keyboard_binding(path: Path | None, key: str) -> str:
    if path and path.exists():
        section = ""
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if section != "Pad1" or "=" not in line:
                continue
            current_key, value = (part.strip() for part in line.split("=", 1))
            if current_key == key and value.startswith("Keyboard/"):
                return value
    return _DEFAULT_BINDINGS[key]


def _vk_from_binding(binding: str) -> int:
    if " & " in binding:
        raise ValueError(f"keyboard chords are not supported for background pad input: {binding}")
    name = binding.removeprefix("Keyboard/")
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1:
        value = int(user32.VkKeyScanW(name.upper()))
        if value < 0:
            raise ValueError(f"could not map keyboard binding {binding!r}")
        return value & 0xFF
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x6F + int(name[1:])
    raise ValueError(f"unsupported keyboard binding {binding!r}")


def _post_key(hwnd: int, vk: int, pressed: bool) -> None:
    scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
    extended = 1 << 24 if vk in _EXTENDED_KEYS else 0
    lparam = 1 | (scan << 16) | extended
    message = WM_KEYDOWN
    if not pressed:
        message = WM_KEYUP
        lparam |= (1 << 30) | (1 << 31)
    if not user32.PostMessageW(ctypes.c_void_p(hwnd), message, vk, lparam):
        raise OSError(ctypes.get_last_error(), f"PostMessageW failed for 0x{hwnd:X}")


class BackgroundKeyboard:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._held: dict[tuple[int, str], int] = {}

    def _binding(self, name: str, config_path: Path | None) -> tuple[str, int]:
        key = _BUTTON_KEYS[name]
        binding = _read_keyboard_binding(config_path, key)
        return binding, _vk_from_binding(binding)

    def press(self, hwnd: int, button: str, duration_ms: int, config_path: Path | None) -> dict[str, Any]:
        name = normalize_button(button)
        if not 1 <= duration_ms <= 10_000:
            raise ValueError("duration_ms must be between 1 and 10000")
        binding, vk = self._binding(name, config_path)
        held_key = (hwnd, name)
        with self._lock:
            was_held = held_key in self._held
            if not was_held:
                _post_key(hwnd, vk, True)
            try:
                time.sleep(duration_ms / 1000.0)
            finally:
                if not was_held:
                    _post_key(hwnd, vk, False)
            return {
                "backend": "keyboard/PostMessageW",
                "button": name,
                "binding": binding,
                "duration_ms": duration_ms,
                "hwnd": f"0x{hwnd:X}",
                "foreground_activation": False,
            }

    def set_buttons(
        self,
        hwnd: int,
        buttons: dict[str, bool | None],
        config_path: Path | None,
    ) -> dict[str, Any]:
        updates: list[tuple[str, bool, str, int]] = []
        for button, pressed in buttons.items():
            name = normalize_button(button)
            if pressed is None:
                continue
            if not isinstance(pressed, bool):
                raise ValueError(f"button state for {button!r} must be true, false, or null")
            binding, vk = self._binding(name, config_path)
            updates.append((name, pressed, binding, vk))
        with self._lock:
            for name, pressed, _binding, vk in updates:
                held_key = (hwnd, name)
                if pressed and held_key not in self._held:
                    _post_key(hwnd, vk, True)
                    self._held[held_key] = vk
                elif not pressed and held_key in self._held:
                    _post_key(hwnd, self._held.pop(held_key), False)
            return {
                "backend": "keyboard/PostMessageW",
                "hwnd": f"0x{hwnd:X}",
                "held": sorted(name for (held_hwnd, name) in self._held if held_hwnd == hwnd),
                "bindings": {name: binding for name, _pressed, binding, _vk in updates},
                "foreground_activation": False,
            }

    def set_analog(
        self,
        hwnd: int,
        x: float,
        y: float,
        stick: str,
        config_path: Path | None,
    ) -> dict[str, Any]:
        side = stick.strip().lower()
        if side not in {"left", "right"}:
            raise ValueError("stick must be 'left' or 'right'")
        if not -1.0 <= x <= 1.0 or not -1.0 <= y <= 1.0:
            raise ValueError("x and y must be between -1.0 and 1.0")
        prefix = "L" if side == "left" else "R"
        states = {
            f"{side}_left": (f"{prefix}Left", x < -0.25),
            f"{side}_right": (f"{prefix}Right", x > 0.25),
            f"{side}_up": (f"{prefix}Up", y > 0.25),
            f"{side}_down": (f"{prefix}Down", y < -0.25),
        }
        with self._lock:
            for state_name, (key, pressed) in states.items():
                binding = _read_keyboard_binding(config_path, key)
                vk = _vk_from_binding(binding)
                held_key = (hwnd, state_name)
                if pressed and held_key not in self._held:
                    _post_key(hwnd, vk, True)
                    self._held[held_key] = vk
                elif not pressed and held_key in self._held:
                    _post_key(hwnd, self._held.pop(held_key), False)
            return {
                "backend": "keyboard/PostMessageW",
                "hwnd": f"0x{hwnd:X}",
                "stick": side,
                "x": x,
                "y": y,
                "foreground_activation": False,
            }

    def reset(self, hwnd: int) -> dict[str, Any]:
        with self._lock:
            for held_key, vk in list(self._held.items()):
                if held_key[0] != hwnd:
                    continue
                _post_key(hwnd, vk, False)
                self._held.pop(held_key, None)
            return {
                "backend": "keyboard/PostMessageW",
                "hwnd": f"0x{hwnd:X}",
                "held": [],
                "foreground_activation": False,
            }


keyboard = BackgroundKeyboard()
