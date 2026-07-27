# DuckStation MCP

**ENGLISH | [KOREAN](README_ko.md)**

DuckStation MCP is a Windows-focused Model Context Protocol server for debugging PlayStation games through [DuckStation](https://github.com/stenzek/duckstation)'s built-in GDB Remote Serial Protocol server. It exposes execution control, register access, memory inspection, breakpoints, process management, and log analysis as MCP tools.

> [!IMPORTANT]
> Basic debugging uses DuckStation's existing GDB server. Native background input and rendered-frame screenshots require the companion patched DuckStation build that implements the `qDuckStation` commands used by this server.

## Features

- Connect to DuckStation's GDB server and inspect connection status.
- Pause, resume, and single-step emulation.
- Read MIPS registers and list supported register names.
- Read, write, and dump emulated memory.
- Dump preset RAM, scratchpad, and BIOS regions.
- Add and remove execution, read, write, and access breakpoints.
- Locate, tail, and filter DuckStation logs by level, channel, or text.
- List and terminate DuckStation processes when files are locked.
- Capture covered or inactive DuckStation windows without activating them.
- Send controller actions through native `qDuckStation` input, an optional virtual XInput pad, or targeted keyboard messages.
- Capture the rendered frame natively through `qDuckStation`, with inactive-window fallback.

## Requirements

- Windows
- Python 3.10 or newer
- DuckStation with its GDB server enabled
- An MCP client that supports local stdio servers

The default GDB endpoint is `127.0.0.1:19000`.

## DuckStation setup

DuckStation normally stores its Windows configuration at `%LOCALAPPDATA%\DuckStation\settings.ini`. Enable the GDB server and file logging:

```ini
[Debug]
EnableGDBServer = true
GDBServerPort = 19000

[Logging]
LogToFile = true
```

The equivalent GUI options are under **Settings > Advanced > Debugging (Tweaks)**. Restart DuckStation after changing the settings, then load a game before calling `connect`.

The default log path is `%LOCALAPPDATA%\DuckStation\duckstation.log`.

## Background capture and input

`configure_background_input` preserves existing Pad 1 bindings, enables
DuckStation background input, and adds keyboard plus XInput mappings. Restart
DuckStation only when the tool reports `restart_required=true`.

The input tools accept `backend="auto"`, `"native"`, `"keyboard"`, or
`"xinput"`. `auto` uses an already-connected `qDuckStation` GDB client first,
then tries the optional XInput backend, and finally falls back to targeted
`PostMessageW` keyboard events when `vgamepad` or its driver is unavailable.
The keyboard path posts directly to DuckStation's native render child and does
not activate, restore, or foreground the window.

`take_screenshot` captures the rendered frame through `qDuckStation` when GDB
is connected. Otherwise it falls back to `capture_duckstation_window`, which
uses `PrintWindow(PW_RENDERFULLCONTENT)` without changing the target window
state. Neither path brings DuckStation forward.

## Installation

Open PowerShell in the repository directory:

```powershell
python -m pip install -e .
```

The keyboard backend has no extra dependency. Optional XInput support uses
`vgamepad` and an installed ViGEmBus driver:

```powershell
python -m pip install -e ".[xinput]"
```

Run the server directly:

```powershell
python -m duckstation_mcp
```

The server communicates over stdio, so it normally runs under an MCP client rather than in a standalone interactive terminal.

## Codex MCP configuration

To register DuckStation MCP as a local stdio server in Codex, add the following blocks to `%USERPROFILE%\.codex\config.toml`. Replace `C:\path\to\Duckstation_MCP` with the path where you cloned this repository.

```toml
[mcp_servers.duckstation]
command = "python"
args = ["-m", "duckstation_mcp"]
cwd = 'C:\path\to\Duckstation_MCP'

[mcp_servers.duckstation.env]
PYTHONPATH = 'C:\path\to\Duckstation_MCP\src'
```

The `python` command must resolve to the interpreter where the project dependencies were installed. Restart Codex after changing `config.toml`.

## Typical workflow

1. Enable DuckStation's GDB server and load a game.
2. Call `connect` using port `19000`. The default `auto_resume=true` resumes emulation after DuckStation pauses for the new GDB client.
3. Use `pause` before inspecting registers or single-stepping.
4. Inspect state with `get_registers` and `read_memory`.
5. Use `set_breakpoint`, `step`, and `resume` to control execution.
6. Use `dump_memory` or `dump_region` when a binary memory dump is required.
7. Call `disconnect` before shutting down DuckStation.

If DuckStation is holding a file lock, call `kill_duckstation`. It first requests normal termination and escalates to a forced kill after the timeout. Set `force=true` to kill it immediately.

## Memory region presets

| Name | Address | Size |
| --- | ---: | ---: |
| `ram` | `0x00000000` | 2 MiB |
| `ram_kseg0` | `0x80000000` | 2 MiB |
| `ram_kseg1` | `0xA0000000` | 2 MiB |
| `scratch` | `0x1F800000` | 1 KiB |
| `bios` | `0x1FC00000` | 512 KiB |

Use `list_memory_regions` to inspect these presets and `dump_region` to write one to a file.

## Operational notes

- DuckStation pauses emulation when a GDB client connects. `connect` defaults to `auto_resume=true`, but the connection can still cause a short visible interruption.
- Large memory dumps are split into `0x7000`-byte requests to keep hex-encoded GDB replies below the socket response limit.
- Memory reads and writes can work while the game is running. Pause the game for stable register inspection and single-step operations.
- A breakpoint hit produces an asynchronous stop reply. The client consumes that reply during the next MCP operation.

## Safety

This server can modify emulated memory, alter execution flow, write memory dumps to arbitrary paths, and terminate DuckStation processes. Keep it bound to local MCP clients and review tool arguments before approving destructive operations.

Generated logs, dumps, virtual environments, bytecode, and build outputs should remain outside version control.

## License

This project is distributed under the terms in [LICENSE](LICENSE).
