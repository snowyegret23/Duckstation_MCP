"""Find and terminate DuckStation processes.

Used when a patch tool can't overwrite a file because DuckStation still has it
open. Matches any running executable whose image name starts with 'duckstation'.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import psutil


def _no_activate_startupinfo() -> subprocess.STARTUPINFO | None:
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is None:
        return None
    startupinfo = startupinfo_type()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_SHOWNOACTIVATE", 4)
    return startupinfo


def _matches(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return lowered.startswith("duckstation") or "duckstation" in lowered


def find_processes() -> list[dict]:
    out: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            info = p.info
            if _matches(info.get("name")) or _matches(info.get("exe")):
                out.append(
                    {
                        "pid": info["pid"],
                        "name": info.get("name"),
                        "exe": info.get("exe"),
                        "created": info.get("create_time"),
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def terminate_processes(force: bool = False, timeout: float = 5.0) -> dict:
    """Graceful terminate (SIGTERM / Ctrl-Break on Windows), escalate to kill if needed.

    When ``force`` is true we skip the graceful step and call kill() directly.
    """
    procs: list[psutil.Process] = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if _matches(p.info.get("name")) or _matches(p.info.get("exe")):
                procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    killed: list[int] = []
    failed: list[dict] = []

    if not procs:
        return {"killed": [], "failed": [], "note": "no DuckStation processes found"}

    for p in procs:
        pid = p.pid
        try:
            if force:
                p.kill()
            else:
                p.terminate()
        except psutil.NoSuchProcess:
            killed.append(pid)
            continue
        except psutil.AccessDenied as e:
            failed.append({"pid": pid, "error": f"access denied: {e}"})
            continue
        except Exception as e:
            failed.append({"pid": pid, "error": str(e)})
            continue

    # Wait and escalate.
    deadline = time.monotonic() + timeout
    for p in procs:
        if p.pid in killed:
            continue
        remaining = max(0.1, deadline - time.monotonic())
        try:
            p.wait(timeout=remaining)
            killed.append(p.pid)
        except psutil.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=2.0)
                killed.append(p.pid)
            except Exception as e:
                failed.append({"pid": p.pid, "error": f"kill escalation failed: {e}"})
        except psutil.NoSuchProcess:
            killed.append(p.pid)
        except Exception as e:
            failed.append({"pid": p.pid, "error": str(e)})

    return {"killed": killed, "failed": failed}


def launch(exe_path: str, game_path: str | None = None, extra_args: list[str] | None = None) -> dict:
    exe = Path(exe_path)
    if not exe.exists():
        return {"ok": False, "error": f"DuckStation executable not found: {exe}"}
    args = [str(exe)]
    if game_path:
        args.append(str(Path(game_path)))
    if extra_args:
        args.extend(extra_args)

    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"duckstation-{time.strftime('%Y%m%d-%H%M%S')}.log"
    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
            startupinfo=_no_activate_startupinfo(),
        )
    finally:
        log.close()
    return {
        "ok": True,
        "pid": proc.pid,
        "args": args,
        "log_path": str(log_path),
        "foreground_activation": False,
        "show_policy": "SW_SHOWNOACTIVATE",
    }
