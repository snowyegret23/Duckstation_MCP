"""Find and terminate DuckStation processes.

Used when a patch tool can't overwrite a file because DuckStation still has it
open. Matches any running executable whose image name starts with 'duckstation'.
"""

from __future__ import annotations

import time

import psutil


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
