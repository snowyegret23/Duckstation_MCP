"""GDB Remote Serial Protocol client for DuckStation's built-in GDB server.

DuckStation's server is MIPS III, lives in src/core/gdb_server.cpp, and supports:
    ?, g, G, H, m, M, s, z/Z, plus raw 'c' (continue) and '\\x03' (interrupt) handled in OnRead.

Registers in reply to 'g': 38 real MIPS regs (r0..r31, SR, LO, HI, BadVaddr, Cause, PC)
followed by 35 zero-padded FP-stub regs for a total of NUM_GDB_REGISTERS = 73.
Each register is 4 bytes little-endian, encoded as 8 lowercase hex chars.

The server auto-pauses the emulator when a client connects, so after connect we
have paused state. Use resume() to let the game continue, pause() to interrupt.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

REG_NAMES: tuple[str, ...] = (
    "zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
    "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
    "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
    "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra",
    "sr", "lo", "hi", "badvaddr", "cause", "pc",
)

BP_TYPE_MAP = {
    "sw": 0,
    "exec": 1,
    "hw": 1,
    "write": 2,
    "read": 3,
    "rw": 4,
}

# DuckStation sends each RSP reply with a single socket write capped at 65536
# bytes. Memory replies hex-encode every byte, so 0x7000 leaves enough room for
# framing while reducing a 2 MiB RAM dump from 2048 requests to 74.
DEFAULT_MEMORY_READ_CHUNK = 0x7000


@dataclass
class PacketReply:
    payload: bytes

    def as_str(self) -> str:
        return self.payload.decode("ascii", errors="replace")


class GDBError(RuntimeError):
    pass


class GDBClient:
    """Thread-safe GDB RSP client.

    Single socket, one command at a time. A lock serialises send/recv so the
    MCP server can call methods from concurrent tool invocations safely.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._buffer: bytes = b""
        self._lock = threading.Lock()
        self._host: str | None = None
        self._port: int | None = None
        self._running: bool = False  # True after resume(), False after pause()/connect()

    # ------------------------------------------------------------------
    # Socket lifecycle
    # ------------------------------------------------------------------
    def connect(self, host: str = "127.0.0.1", port: int = 19000, timeout: float = 5.0) -> None:
        with self._lock:
            if self._sock is not None:
                raise GDBError("Already connected; call disconnect() first")
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            self._sock = sock
            self._buffer = b""
            self._host = host
            self._port = port
            # Server auto-pauses on connect; m_seen_resume is false, so no spontaneous packet.
            self._running = False

    def disconnect(self) -> None:
        with self._lock:
            self._disconnect_locked()

    def _disconnect_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""
        self._running = False

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self._host,
            "port": self._port,
            "running": self._running,
        }

    # ------------------------------------------------------------------
    # Raw framing
    # ------------------------------------------------------------------
    @staticmethod
    def _checksum(payload: bytes) -> bytes:
        s = 0
        for b in payload:
            s = (s + b) & 0xFF
        return f"{s:02x}".encode("ascii")

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise GDBError("Not connected to DuckStation GDB server")
        return self._sock

    def _send_raw(self, data: bytes) -> None:
        sock = self._require_sock()
        try:
            sock.sendall(data)
        except OSError as e:
            self._disconnect_locked()
            raise GDBError(f"socket send failed: {e}") from e

    def _recv_more(self, timeout: float) -> None:
        sock = self._require_sock()
        sock.settimeout(max(0.05, timeout))
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise TimeoutError("timed out waiting for GDB server") from None
        except OSError as e:
            self._disconnect_locked()
            raise GDBError(f"socket recv failed: {e}") from e
        if not chunk:
            self._disconnect_locked()
            raise GDBError("GDB server closed the connection")
        self._buffer += chunk

    def _drain_acks(self) -> int:
        """Drop leading + / - bytes from the buffer. Returns how many '-' we saw."""
        neg = 0
        while self._buffer and self._buffer[0:1] in (b"+", b"-"):
            if self._buffer[0:1] == b"-":
                neg += 1
            self._buffer = self._buffer[1:]
        return neg

    def _read_packet(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            self._drain_acks()
            if self._buffer.startswith(b"$"):
                hash_idx = self._buffer.find(b"#", 1)
                if hash_idx != -1 and len(self._buffer) >= hash_idx + 3:
                    payload = self._buffer[1:hash_idx]
                    cs = self._buffer[hash_idx + 1 : hash_idx + 3]
                    self._buffer = self._buffer[hash_idx + 3 :]
                    expected = self._checksum(payload)
                    if cs.lower() != expected.lower():
                        self._send_raw(b"-")
                        raise GDBError(
                            f"checksum mismatch: got {cs!r}, expected {expected!r}"
                        )
                    self._send_raw(b"+")
                    return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out reading packet from GDB server")
            self._recv_more(timeout=remaining)

    def _send_packet(self, payload: bytes) -> None:
        cs = self._checksum(payload)
        self._send_raw(b"$" + payload + b"#" + cs)

    def _try_parse_packet(self) -> bytes | None:
        """Try to extract one complete packet from ``self._buffer`` without blocking.

        Drains leading +/- acks. Returns the raw payload (without framing) if a
        full ``$...#xx`` packet is already buffered, else ``None``. When a packet
        is returned, its ack '+' has been sent back to the server.
        """
        self._drain_acks()
        if not self._buffer.startswith(b"$"):
            return None
        hash_idx = self._buffer.find(b"#", 1)
        if hash_idx == -1 or len(self._buffer) < hash_idx + 3:
            return None
        payload = self._buffer[1:hash_idx]
        cs = self._buffer[hash_idx + 1 : hash_idx + 3]
        self._buffer = self._buffer[hash_idx + 3 :]
        expected = self._checksum(payload)
        if cs.lower() != expected.lower():
            self._send_raw(b"-")
            raise GDBError(f"checksum mismatch: got {cs!r}, expected {expected!r}")
        self._send_raw(b"+")
        return payload

    def _drain_pending_stops(self, poll_timeout: float = 0.02) -> bool:
        """Non-blocking: consume any spontaneous stop replies (S/T) the server
        sent while we were running (watchpoint hit, BP hit, trap).

        Updates ``self._running`` to False if one is found. Returns True iff at
        least one stop reply was drained. Safe to call while holding the lock.
        """
        drained = False
        sock = self._require_sock()
        # Opportunistically read whatever's already in the socket.
        old_to = sock.gettimeout()
        sock.settimeout(poll_timeout)
        try:
            while True:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, BlockingIOError):
                    break
                if not chunk:
                    break
                self._buffer += chunk
        finally:
            try:
                sock.settimeout(old_to)
            except OSError:
                pass
        # Parse & drop any stop replies (S/T). A stop reply indicates the CPU
        # paused itself; leave the non-stop packet (if any) untouched for the
        # caller's next _read_packet() to pick up.
        while True:
            # Peek without consuming non-stop packets.
            self._drain_acks()
            if not self._buffer.startswith(b"$"):
                break
            hash_idx = self._buffer.find(b"#", 1)
            if hash_idx == -1 or len(self._buffer) < hash_idx + 3:
                break
            payload = self._buffer[1:hash_idx]
            if not payload.startswith((b"S", b"T")):
                # Not a stop reply — leave it for _read_packet().
                break
            # Consume the stop reply.
            self._buffer = self._buffer[hash_idx + 3 :]
            self._send_raw(b"+")
            self._running = False
            drained = True
        return drained

    def _command(self, payload: bytes, timeout: float = 5.0) -> bytes:
        # Drain any spontaneous stop replies queued from a prior watchpoint /
        # BP hit so our command's real reply isn't preceded by stale 'S00'.
        try:
            self._drain_pending_stops(poll_timeout=0.02)
        except GDBError:
            # Don't block a legitimate command on a transient recv error.
            pass
        self._send_packet(payload)
        return self._read_packet(timeout=timeout)

    # ------------------------------------------------------------------
    # Execution control
    # ------------------------------------------------------------------
    def pause(self, timeout: float = 2.0) -> str:
        """Send interrupt. If we were running, expect an S00 stop reply;
        otherwise swallow silently because the server won't respond.

        If the CPU has already paused itself (e.g. watchpoint hit) before we
        asked, the server has queued an S00/T-prefixed stop reply. We detect
        and consume it so subsequent commands aren't desynchronised.
        """
        with self._lock:
            self._require_sock()
            # First check whether the server already sent a stop reply (e.g.
            # watchpoint fired while we were running). If so, don't interrupt —
            # just drain it and report paused.
            try:
                if self._drain_pending_stops(poll_timeout=0.05):
                    self._running = False
                    return "S00"  # synthesised normalised reply
            except GDBError:
                pass
            # Still running per our state. Send interrupt.
            self._send_raw(b"\x03")
            if not self._running:
                return "already-paused"
            try:
                reply = self._read_packet(timeout=timeout)
            except TimeoutError:
                self._running = False
                return "no-reply"
            self._running = False
            return reply.decode("ascii", errors="replace")

    def resume(self) -> None:
        """Send continue. Server does not send a packet reply; it sends a bare '+' ack
        via OnSystemResumed. We record the new state and let the buffered ack be
        consumed by the next _read_packet() call.
        """
        with self._lock:
            self._require_sock()
            self._send_packet(b"c")
            self._running = True

    def step(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._require_sock()
            reply = self._command(b"s", timeout=timeout)
            if reply != b"OK":
                raise GDBError(f"step failed: {reply!r}")
            # After single-step the CPU is paused again.
            self._running = False

    # ------------------------------------------------------------------
    # Registers
    # ------------------------------------------------------------------
    def get_registers(self) -> dict[str, int]:
        with self._lock:
            reply = self._command(b"g", timeout=5.0)
        if len(reply) < len(REG_NAMES) * 8:
            raise GDBError(f"short 'g' reply: {len(reply)} chars")
        result: dict[str, int] = {}
        for i, name in enumerate(REG_NAMES):
            hex_bytes = reply[i * 8 : (i + 1) * 8].decode("ascii")
            word_le = bytes.fromhex(hex_bytes)
            result[name] = int.from_bytes(word_le, "little")
        return result

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def read_memory(self, address: int, length: int, timeout: float = 15.0) -> bytes:
        if length <= 0:
            return b""
        with self._lock:
            cmd = f"m{address & 0xFFFFFFFF:x},{length:x}".encode("ascii")
            reply = self._command(cmd, timeout=timeout)
        if reply.startswith(b"E"):
            raise GDBError(
                f"read_memory(0x{address:08X}, {length}) failed: {reply.decode('ascii', errors='replace')}"
            )
        try:
            return bytes.fromhex(reply.decode("ascii"))
        except ValueError as e:
            raise GDBError(f"invalid hex in memory reply: {e}") from e

    def read_memory_chunked(
        self,
        address: int,
        length: int,
        chunk: int = DEFAULT_MEMORY_READ_CHUNK,
        timeout: float = 30.0,
    ) -> bytes:
        """Read a large range without exceeding DuckStation's single-write reply cap."""
        if chunk <= 0:
            raise ValueError("memory read chunk must be positive")
        out = bytearray()
        remaining = length
        cur = address
        while remaining > 0:
            take = min(chunk, remaining)
            out.extend(self.read_memory(cur, take, timeout=timeout))
            cur += take
            remaining -= take
        return bytes(out)

    def write_memory(self, address: int, data: bytes, timeout: float = 15.0) -> None:
        with self._lock:
            hex_data = data.hex()
            cmd = f"M{address & 0xFFFFFFFF:x},{len(data):x}:{hex_data}".encode("ascii")
            reply = self._command(cmd, timeout=timeout)
        if reply != b"OK":
            raise GDBError(
                f"write_memory(0x{address:08X}, {len(data)}B) failed: {reply.decode('ascii', errors='replace')}"
            )

    # ------------------------------------------------------------------
    # Breakpoints
    # ------------------------------------------------------------------
    def _bp_type(self, kind: str) -> int:
        if kind not in BP_TYPE_MAP:
            raise GDBError(f"unknown breakpoint kind {kind!r}; use one of {list(BP_TYPE_MAP)}")
        return BP_TYPE_MAP[kind]

    def set_breakpoint(self, address: int, kind: str = "exec") -> None:
        t = self._bp_type(kind)
        with self._lock:
            cmd = f"Z{t},{address & 0xFFFFFFFF:x}".encode("ascii")
            reply = self._command(cmd, timeout=5.0)
        if reply != b"OK":
            raise GDBError(
                f"set_breakpoint(0x{address:08X}, {kind}) failed: {reply.decode('ascii', errors='replace')}"
            )

    def remove_breakpoint(self, address: int, kind: str = "exec") -> None:
        t = self._bp_type(kind)
        with self._lock:
            cmd = f"z{t},{address & 0xFFFFFFFF:x}".encode("ascii")
            reply = self._command(cmd, timeout=5.0)
        if reply != b"OK":
            raise GDBError(
                f"remove_breakpoint(0x{address:08X}, {kind}) failed: {reply.decode('ascii', errors='replace')}"
            )

    def duckstation_command(self, command: str, timeout: float = 30.0) -> str:
        with self._lock:
            reply = self._command(f"qDuckStation:{command}".encode("ascii"), timeout=timeout)
        text = reply.decode("ascii", errors="replace")
        if text.startswith("ERR"):
            raise GDBError(text)
        return text
