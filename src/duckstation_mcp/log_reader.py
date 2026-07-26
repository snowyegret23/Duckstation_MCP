"""Read DuckStation's duckstation.log file.

DuckStation formats lines as ``[  12.3456] L/Channel: message`` where L is one
letter from E/W/I/V/D/X/T (Error/Warning/Info/Verbose/Dev/Debug/Trace).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Windows-only default candidates. We try %LOCALAPPDATA%\DuckStation first,
# then the legacy Documents location, then the portable location next to the exe.
LOCALAPPDATA_LOG = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    / "DuckStation"
    / "duckstation.log"
)
DOCUMENTS_LOG = Path.home() / "Documents" / "DuckStation" / "duckstation.log"

LEVEL_LETTERS = {
    "error": "E",
    "warning": "W",
    "info": "I",
    "verbose": "V",
    "dev": "D",
    "debug": "X",
    "trace": "T",
}

LINE_RE = re.compile(r"^\[\s*[\d.]+\]\s+([EWIVDXT])/([^:]+):")


def resolve_log_path(custom: str | None = None) -> Path:
    if custom:
        return Path(custom)
    for candidate in (LOCALAPPDATA_LOG, DOCUMENTS_LOG):
        if candidate.exists():
            return candidate
    return LOCALAPPDATA_LOG  # return the expected path even if missing


def _read_last_lines(path: Path, lines: int) -> list[str]:
    """Return up to ``lines`` trailing lines without loading the entire file."""
    if lines <= 0:
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            read = min(chunk, size)
            size -= read
            f.seek(size)
            data = f.read(read) + data
    text = data.decode("utf-8", errors="replace").splitlines()
    return text[-lines:]


def tail_log(lines: int = 100, path: str | None = None) -> dict:
    p = resolve_log_path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "lines": []}
    return {"path": str(p), "exists": True, "lines": _read_last_lines(p, lines)}


def filter_log(
    levels: tuple[str, ...] = ("error", "warning"),
    lines: int = 500,
    channel: str | None = None,
    contains: str | None = None,
    path: str | None = None,
) -> dict:
    """Scan the log and keep lines matching the given levels/channel/substring.

    Parameters
    ----------
    levels : tuple of level names (error, warning, info, verbose, dev, debug, trace)
    lines  : max result lines (most recent kept)
    channel: restrict to one channel name (case-insensitive substring match)
    contains: additional substring filter applied to the message text
    """
    p = resolve_log_path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "lines": []}

    wanted_letters = {LEVEL_LETTERS[lvl.lower()] for lvl in levels if lvl.lower() in LEVEL_LETTERS}
    channel_needle = channel.lower() if channel else None
    contains_needle = contains.lower() if contains else None

    kept: list[str] = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = LINE_RE.match(raw)
            if not m:
                continue
            letter, chan = m.group(1), m.group(2).strip()
            if wanted_letters and letter not in wanted_letters:
                continue
            if channel_needle and channel_needle not in chan.lower():
                continue
            if contains_needle and contains_needle not in raw.lower():
                continue
            kept.append(raw.rstrip("\n"))

    return {
        "path": str(p),
        "exists": True,
        "total_matches": len(kept),
        "lines": kept[-lines:] if lines > 0 else kept,
    }
