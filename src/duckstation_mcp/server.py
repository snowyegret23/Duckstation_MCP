"""MCP server exposing DuckStation debug/memory/log tools.

The singleton `mcp` is launched by `__main__.py`. Tool handlers translate
simple argument types into GDB RSP commands via `GDBClient`, read the log file
via `log_reader`, and control the DuckStation process via `process`.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import background_input
from . import keyboard_input
from . import process as ds_process
from . import window_capture
from .gdb_client import BP_TYPE_MAP, GDBClient, GDBError, REG_NAMES
from .log_reader import filter_log, resolve_log_path, tail_log

DEFAULT_PORT = 19000

# PSX memory regions of interest for dumps.
PSX_REGIONS: dict[str, tuple[int, int]] = {
    "ram":      (0x00000000, 0x00200000),   # 2 MB main RAM
    "ram_kseg0":(0x80000000, 0x00200000),   # RAM mirror through KSEG0 (cached)
    "ram_kseg1":(0xA0000000, 0x00200000),   # RAM mirror through KSEG1 (uncached)
    "scratch":  (0x1F800000, 0x00000400),   # 1 KB scratchpad
    "bios":     (0x1FC00000, 0x00080000),   # 512 KB BIOS ROM
}

mcp = FastMCP("duckstation-mcp")

# Single GDB connection shared across tool calls.
_client = GDBClient()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_NATIVE_BUTTON_BINDINGS = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "triangle": "Triangle",
    "circle": "Circle",
    "cross": "Cross",
    "square": "Square",
    "select": "Select",
    "start": "Start",
    "l1": "L1",
    "l2": "L2",
    "l3": "L3",
    "r1": "R1",
    "r2": "R2",
    "r3": "R3",
}


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def _err(msg: str, **kw: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **kw}


def _select_input_backend(backend: str) -> tuple[str, str | None]:
    requested = backend.strip().lower()
    if requested not in {"auto", "native", "keyboard", "xinput"}:
        raise ValueError("backend must be 'auto', 'native', 'keyboard', or 'xinput'")
    if requested == "native":
        if not _client.connected:
            raise GDBError("native input requires an active DuckStation GDB connection")
        return "native", None
    if requested == "auto" and _client.connected:
        return "native", None
    selected, fallback_reason = background_input.choose_backend(requested)
    if requested == "auto" and fallback_reason is None:
        fallback_reason = "native input unavailable because the GDB client is not connected"
    elif requested == "auto":
        fallback_reason = (
            "native input unavailable because the GDB client is not connected; "
            f"{fallback_reason}"
        )
    return selected, fallback_reason


def _native_input_set(controller: int, binding: str, value: float) -> str:
    if controller < 0:
        raise ValueError("controller must be non-negative")
    if not 0.0 <= value <= 1.0:
        raise ValueError("native input value must be between 0.0 and 1.0")
    value_milli = int(round(value * 1000.0))
    return _client.duckstation_command(f"input:{controller}:{binding}:{value_milli}")


def _native_button_name(button: str) -> tuple[str, str]:
    normalized = background_input.normalize_button(button)
    binding = _NATIVE_BUTTON_BINDINGS.get(normalized)
    if binding is None:
        raise ValueError(f"button {button!r} is not available on the active PlayStation controller")
    return normalized, binding


def _native_analog_values(x: float, y: float, stick: str) -> dict[str, float]:
    side = stick.strip().lower()
    if side not in {"left", "right"}:
        raise ValueError("stick must be 'left' or 'right'")
    if not -1.0 <= x <= 1.0 or not -1.0 <= y <= 1.0:
        raise ValueError("x and y must be between -1.0 and 1.0")
    prefix = "L" if side == "left" else "R"
    return {
        f"{prefix}Left": max(0.0, -x),
        f"{prefix}Right": max(0.0, x),
        f"{prefix}Up": max(0.0, y),
        f"{prefix}Down": max(0.0, -y),
    }


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------
@mcp.tool()
def connect(port: int = DEFAULT_PORT, host: str = "127.0.0.1", auto_resume: bool = True) -> dict:
    """Open a GDB RSP connection to DuckStation.

    DuckStation auto-pauses the emulator when a GDB client connects. Set
    ``auto_resume=True`` (default) to immediately resume the game so debugging
    can proceed without the game freezing.

    Pre-requisite: in DuckStation settings.ini set ``[Debug] EnableGDBServer = true``
    and ``GDBServerPort = 19000`` (or whatever port you pass here), then restart.
    """
    try:
        _client.connect(host=host, port=port)
    except (OSError, GDBError) as e:
        return _err(f"connect failed: {e}", host=host, port=port,
                    hint="Check that DuckStation is running and EnableGDBServer=true in settings.ini")
    if auto_resume:
        try:
            _client.resume()
        except GDBError as e:
            return _err(f"connected but resume failed: {e}")
    return _ok(**_client.status())


@mcp.tool()
def disconnect() -> dict:
    """Close the GDB RSP connection. Does not affect the running emulator."""
    _client.disconnect()
    return _ok(**_client.status())


@mcp.tool()
def status() -> dict:
    """Current connection state: connected host/port and whether the emu is running."""
    return _ok(**_client.status())


# ----------------------------------------------------------------------
# Execution control
# ----------------------------------------------------------------------
@mcp.tool()
def pause() -> dict:
    """Interrupt the emulator CPU. Required for register inspection / single-step."""
    try:
        reply = _client.pause()
    except GDBError as e:
        return _err(str(e))
    return _ok(reply=reply, **_client.status())


@mcp.tool()
def resume() -> dict:
    """Resume emulation. Memory reads still work while running."""
    try:
        _client.resume()
    except GDBError as e:
        return _err(str(e))
    return _ok(**_client.status())


@mcp.tool()
def step() -> dict:
    """Single-step one MIPS instruction. The CPU stays paused afterwards."""
    try:
        _client.step()
    except GDBError as e:
        return _err(str(e))
    return _ok(**_client.status())


# ----------------------------------------------------------------------
# Registers
# ----------------------------------------------------------------------
@mcp.tool()
def get_registers() -> dict:
    """Return MIPS registers as name→value. Emulator should be paused for stable values."""
    try:
        regs = _client.get_registers()
    except GDBError as e:
        return _err(str(e))
    pretty = {k: f"0x{v:08X}" for k, v in regs.items()}
    return _ok(registers=regs, hex=pretty)


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------
def _parse_addr(value: int | str) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    s = value.strip()
    return int(s, 16 if s.lower().startswith("0x") else 0) & 0xFFFFFFFF


@mcp.tool()
def read_memory(address: int | str, length: int, as_format: str = "hex") -> dict:
    """Read ``length`` bytes at ``address``.

    ``address`` accepts either int or hex string ("0x80010000").
    ``as_format`` is one of: "hex" (default), "base64", "ascii". For large regions
    prefer ``dump_memory`` which writes directly to a file.
    """
    try:
        addr = _parse_addr(address)
        data = _client.read_memory_chunked(addr, length)
    except (GDBError, ValueError) as e:
        return _err(str(e))
    payload: dict[str, Any] = {"address": f"0x{addr:08X}", "length": len(data)}
    if as_format == "hex":
        payload["hex"] = data.hex()
    elif as_format == "base64":
        payload["base64"] = base64.b64encode(data).decode("ascii")
    elif as_format == "ascii":
        payload["ascii"] = data.decode("ascii", errors="replace")
    else:
        return _err(f"unknown as_format: {as_format}")
    return _ok(**payload)


@mcp.tool()
def write_memory(address: int | str, data_hex: str) -> dict:
    """Write hex-encoded bytes to ``address``. Length inferred from the hex string."""
    try:
        addr = _parse_addr(address)
        raw = bytes.fromhex(data_hex.replace(" ", ""))
    except ValueError as e:
        return _err(f"invalid hex: {e}")
    try:
        _client.write_memory(addr, raw)
    except GDBError as e:
        return _err(str(e))
    return _ok(address=f"0x{addr:08X}", written=len(raw))


@mcp.tool()
def dump_memory(address: int | str, length: int, file_path: str) -> dict:
    """Dump ``length`` bytes at ``address`` into a binary file on disk."""
    try:
        addr = _parse_addr(address)
        data = _client.read_memory_chunked(addr, length)
    except (GDBError, ValueError) as e:
        return _err(str(e))
    out = Path(file_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return _ok(address=f"0x{addr:08X}", length=len(data), path=str(out.resolve()))


@mcp.tool()
def dump_region(region: str, file_path: str) -> dict:
    """Dump a named PSX memory region to a file.

    Known regions: ram, ram_kseg0, ram_kseg1, scratch, bios.
    """
    if region not in PSX_REGIONS:
        return _err(f"unknown region {region!r}; valid: {list(PSX_REGIONS)}")
    addr, length = PSX_REGIONS[region]
    return dump_memory(addr, length, file_path)


# ----------------------------------------------------------------------
# Breakpoints
# ----------------------------------------------------------------------
@mcp.tool()
def set_breakpoint(address: int | str, kind: str = "exec") -> dict:
    """Set a breakpoint. ``kind`` is one of: exec (default), sw, hw, read, write, rw."""
    try:
        addr = _parse_addr(address)
        _client.set_breakpoint(addr, kind)
    except (GDBError, ValueError) as e:
        return _err(str(e))
    return _ok(address=f"0x{addr:08X}", kind=kind)


@mcp.tool()
def remove_breakpoint(address: int | str, kind: str = "exec") -> dict:
    """Remove a previously-set breakpoint of the same kind."""
    try:
        addr = _parse_addr(address)
        _client.remove_breakpoint(addr, kind)
    except (GDBError, ValueError) as e:
        return _err(str(e))
    return _ok(address=f"0x{addr:08X}", kind=kind)


@mcp.tool()
def list_breakpoint_kinds() -> dict:
    """Return the accepted breakpoint kind strings and their GDB RSP numeric types."""
    return _ok(kinds=dict(BP_TYPE_MAP))


# ----------------------------------------------------------------------
# Logs
# ----------------------------------------------------------------------
@mcp.tool()
def get_log_path(path: str | None = None) -> dict:
    """Return the resolved path to duckstation.log (override with ``path`` if custom)."""
    p = resolve_log_path(path)
    return _ok(path=str(p), exists=p.exists())


@mcp.tool()
def tail_log_lines(lines: int = 100, path: str | None = None) -> dict:
    """Return the last ``lines`` lines of duckstation.log verbatim."""
    return _ok(**tail_log(lines=lines, path=path))


@mcp.tool()
def get_errors(
    lines: int = 200,
    include_warnings: bool = True,
    channel: str | None = None,
    contains: str | None = None,
    path: str | None = None,
) -> dict:
    """Return log entries filtered to Error (and optionally Warning) level.

    Use ``channel`` to restrict to a single log channel substring (e.g. 'GPU',
    'System'), and ``contains`` to further grep the message text.
    """
    levels = ("error", "warning") if include_warnings else ("error",)
    result = filter_log(levels=levels, lines=lines, channel=channel, contains=contains, path=path)
    return _ok(**result)


@mcp.tool()
def filter_log_lines(
    levels: list[str] | None = None,
    lines: int = 200,
    channel: str | None = None,
    contains: str | None = None,
    path: str | None = None,
) -> dict:
    """Flexible log filter. ``levels`` is a list of names from:
    error, warning, info, verbose, dev, debug, trace.
    """
    use_levels = tuple(levels) if levels else ("error", "warning", "info")
    result = filter_log(levels=use_levels, lines=lines, channel=channel, contains=contains, path=path)
    return _ok(**result)


# ----------------------------------------------------------------------
# Process control
# ----------------------------------------------------------------------
@mcp.tool()
def list_duckstation_processes() -> dict:
    """List currently-running DuckStation processes (pid, name, exe path)."""
    return _ok(processes=ds_process.find_processes())


@mcp.tool()
def launch_duckstation(exe_path: str, game_path: str | None = None, extra_args: list[str] | None = None) -> dict:
    """Launch DuckStation without activating its window."""
    return ds_process.launch(exe_path, game_path, extra_args)


@mcp.tool()
def list_duckstation_windows(pid: int | None = None) -> dict:
    """List visible DuckStation windows without changing focus or window state."""
    try:
        if pid is not None:
            return _ok(pid=pid, windows=window_capture.list_windows(pid))
        processes = ds_process.find_processes()
        windows = [window for process in processes for window in window_capture.list_windows(int(process["pid"]))]
        return _ok(windows=windows)
    except OSError as e:
        return _err(str(e), pid=pid)


@mcp.tool()
def capture_duckstation_window(output_path: str | None = None, pid: int | None = None) -> dict:
    """Capture an inactive or covered DuckStation window without restoring or activating it."""
    try:
        if pid is None:
            processes = ds_process.find_processes()
            if not processes:
                return _err("no DuckStation process found")
            pids = [int(process["pid"]) for process in processes]
        else:
            pids = [pid]
        window = window_capture.find_window(pids)
        out = window_capture.default_output_path() if output_path is None else Path(output_path)
        if not out.is_absolute():
            out = Path.cwd() / out
        result = window_capture.capture_window(int(window["hwnd"], 16), out)
        return _ok(pid=window["pid"], title=window["title"], class_name=window["class_name"], **result)
    except (OSError, ValueError, RuntimeError) as e:
        return _err(str(e), pid=pid, output_path=output_path)


@mcp.tool()
def take_screenshot(output_path: str | None = None, pid: int | None = None) -> dict:
    """Capture the emulated frame natively when GDB is connected, otherwise use inactive window capture."""
    try:
        if output_path is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = _PROJECT_ROOT / "logs" / "screenshots" / f"duckstation-{stamp}.png"
        else:
            out = Path(output_path)
            if not out.is_absolute():
                out = Path.cwd() / out
        out = out.resolve()
        if out.suffix.lower() != ".png":
            raise ValueError("DuckStation native screenshots require a .png output path")

        if not _client.connected:
            result = capture_duckstation_window(str(out), pid)
            return {**result, "fallback": "inactive-window"}

        out.parent.mkdir(parents=True, exist_ok=True)
        reply = _client.duckstation_command(f"screenshot:{str(out).encode('utf-8').hex()}", timeout=30.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not out.is_file():
            time.sleep(0.05)
        if not out.is_file():
            return _err(
                "native screenshot command returned but no file was created",
                path=str(out),
                reply=reply,
            )
        return _ok(
            path=str(out),
            size=out.stat().st_size,
            method="qDuckStation screenshot",
            reply=reply,
            foreground_activation=False,
        )
    except (GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), output_path=output_path)


@mcp.tool()
def configure_background_input(config_path: str | None = None, device_index: int = 0) -> dict:
    """Enable background input and add keyboard plus optional XInput bindings."""
    try:
        return _ok(**background_input.configure_background_input(config_path, device_index))
    except (OSError, ValueError) as e:
        return _err(str(e), config_path=config_path, device_index=device_index)


@mcp.tool()
def background_input_status() -> dict:
    """Return native, keyboard, and optional XInput backend state without activating DuckStation."""
    return _ok(
        default_backend="auto",
        auto_order=["native", "xinput", "keyboard"],
        native={"available": _client.connected, "backend": "qDuckStation"},
        xinput_module_available=background_input.xinput_module_available(),
        xinput=background_input.gamepad.status(),
        keyboard_backend="PostMessageW",
        foreground_activation=False,
    )


@mcp.tool()
def input_button_press(
    button: str,
    duration_ms: int = 100,
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Press and release a button through native input, keyboard, or optional XInput without focus."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            if not 1 <= duration_ms <= 10_000:
                raise ValueError("duration_ms must be between 1 and 10000")
            normalized, binding = _native_button_name(button)
            press_reply = _native_input_set(controller, binding, 1.0)
            try:
                time.sleep(duration_ms / 1000.0)
            finally:
                release_reply = _native_input_set(controller, binding, 0.0)
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                controller=controller,
                button=normalized,
                binding=binding,
                duration_ms=duration_ms,
                replies=[press_reply, release_reply],
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(selected_backend=selected, fallback_reason=fallback_reason, **background_input.gamepad.press(button, duration_ms))
        processes = ds_process.find_processes() if pid is None else [{"pid": pid}]
        if not processes:
            return _err("no DuckStation process found")
        window = window_capture.find_window([int(process["pid"]) for process in processes])
        input_window = window_capture.find_input_window(int(window["hwnd"], 16))
        path = Path(config_path) if config_path else next(
            (candidate for candidate in background_input.default_config_candidates() if candidate.exists()),
            None,
        )
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.press(int(input_window["hwnd"], 16), button, duration_ms, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), button=button, backend=backend)


@mcp.tool()
def input_buttons_send(
    buttons: dict[str, bool | None],
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Set multiple button states through native input, keyboard, or optional XInput."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            replies: dict[str, str] = {}
            for button, pressed in buttons.items():
                normalized, binding = _native_button_name(button)
                if pressed is None:
                    continue
                if not isinstance(pressed, bool):
                    raise ValueError(f"button state for {button!r} must be true, false, or null")
                replies[normalized] = _native_input_set(controller, binding, 1.0 if pressed else 0.0)
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                controller=controller,
                replies=replies,
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(selected_backend=selected, fallback_reason=fallback_reason, **background_input.gamepad.set_buttons(buttons))
        processes = ds_process.find_processes() if pid is None else [{"pid": pid}]
        if not processes:
            return _err("no DuckStation process found")
        window = window_capture.find_window([int(process["pid"]) for process in processes])
        input_window = window_capture.find_input_window(int(window["hwnd"], 16))
        path = Path(config_path) if config_path else next(
            (candidate for candidate in background_input.default_config_candidates() if candidate.exists()),
            None,
        )
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.set_buttons(int(input_window["hwnd"], 16), buttons, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), buttons=buttons, backend=backend)


@mcp.tool()
def input_analog_send(
    x: float,
    y: float,
    stick: str = "left",
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Set an analog stick through native input, XInput, or configured keyboard directions."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            values = _native_analog_values(x, y, stick)
            replies = {
                binding: _native_input_set(controller, binding, value)
                for binding, value in values.items()
            }
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                controller=controller,
                stick=stick.strip().lower(),
                x=x,
                y=y,
                replies=replies,
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(selected_backend=selected, fallback_reason=fallback_reason, **background_input.gamepad.set_analog(x, y, stick))
        processes = ds_process.find_processes() if pid is None else [{"pid": pid}]
        if not processes:
            return _err("no DuckStation process found")
        window = window_capture.find_window([int(process["pid"]) for process in processes])
        input_window = window_capture.find_input_window(int(window["hwnd"], 16))
        path = Path(config_path) if config_path else next(
            (candidate for candidate in background_input.default_config_candidates() if candidate.exists()),
            None,
        )
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.set_analog(int(input_window["hwnd"], 16), x, y, stick, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), x=x, y=y, stick=stick, backend=backend)


@mcp.tool()
def input_reset(backend: str = "auto", pid: int | None = None, controller: int = 0) -> dict:
    """Release keyboard and/or virtual controller state without focusing DuckStation."""
    try:
        requested = backend.strip().lower()
        if requested not in {"auto", "native", "keyboard", "xinput"}:
            raise ValueError("backend must be 'auto', 'native', 'keyboard', or 'xinput'")
        native_result = None
        if requested == "native" or (requested == "auto" and _client.connected):
            native_result = _client.duckstation_command(f"input-reset:{controller}")
            if requested == "native":
                return _ok(
                    selected_backend="native",
                    controller=controller,
                    reply=native_result,
                    foreground_activation=False,
                )
        if requested == "xinput":
            return _ok(selected_backend="xinput", **background_input.gamepad.reset())
        processes = ds_process.find_processes() if pid is None else [{"pid": pid}]
        keyboard_result = None
        if processes:
            window = window_capture.find_window([int(process["pid"]) for process in processes])
            input_window = window_capture.find_input_window(int(window["hwnd"], 16))
            keyboard_result = keyboard_input.keyboard.reset(int(input_window["hwnd"], 16))
        xinput_result = (
            background_input.gamepad.reset()
            if requested == "auto" and background_input.gamepad.status()["connected"]
            else None
        )
        return _ok(
            native=native_result,
            keyboard=keyboard_result,
            xinput=xinput_result,
            foreground_activation=False,
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), backend=backend)


@mcp.tool()
def kill_duckstation(force: bool = False, timeout: float = 5.0) -> dict:
    """Terminate all running DuckStation processes.

    Use this when a game file can't be patched because DuckStation still holds
    the file open. ``force=False`` tries a graceful terminate first and escalates
    to kill only on timeout; ``force=True`` skips straight to kill.
    """
    if _client.connected:
        _client.disconnect()
    return _ok(**ds_process.terminate_processes(force=force, timeout=timeout))


# ----------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------
@mcp.tool()
def list_register_names() -> dict:
    """Return MIPS register names in the order get_registers() uses."""
    return _ok(registers=list(REG_NAMES))


@mcp.tool()
def list_memory_regions() -> dict:
    """Return the PSX memory region presets known to dump_region."""
    return _ok(regions={
        name: {"address": f"0x{addr:08X}", "length": length}
        for name, (addr, length) in PSX_REGIONS.items()
    })
