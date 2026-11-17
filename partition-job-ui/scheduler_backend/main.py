"""Entrypoint for the realtime partition-job scheduler backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional

from scheduler_backend.models import SchedulerConfig
from scheduler_backend.scheduler import PartitionScheduler
from scheduler_backend.scheduler_database import load_scheduler_config

logger = logging.getLogger(__name__)


class ProcessSingletonLock:
    """OS-level singleton lock. Prefer fcntl on Linux; fall back on Windows."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        self._windows_lock = None

    def acquire(self) -> bool:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                if self._fh.read(1) == "":
                    self._fh.write("0")
                    self._fh.flush()
                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    self.release()
                    return False
                self._windows_lock = True
                return True

            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt" and self._windows_lock:
                import msvcrt

                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif os.name != "nt":
                import fcntl

                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


_MAX_BODY_BYTES = 4096


async def _read_http_request(reader: asyncio.StreamReader) -> tuple[str, str]:
    method, path, _header = await _read_http_request_full(reader)
    return method, path


async def _read_http_request_full(reader: asyncio.StreamReader) -> tuple[str, str, str]:
    header = await reader.readuntil(b"\r\n\r\n")
    text = header.decode("iso-8859-1", errors="replace")
    first = text.split("\r\n", 1)[0]
    parts = first.split()
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    return method, path, text


async def _read_http_body(reader: asyncio.StreamReader, header_text: str) -> bytes:
    """Read a small JSON body (bounded; Content-Length required)."""
    length = 0
    for line in header_text.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length < 0 or length > _MAX_BODY_BYTES:
        raise ValueError("body too large")
    return await reader.readexactly(length) if length else b""


async def _write_http(
    writer: asyncio.StreamWriter, status: int, body_obj: dict, reason: str = "OK"
) -> None:
    body = json.dumps(body_obj).encode("utf-8")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(header + body)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    scheduler: PartitionScheduler,
) -> None:
    try:
        method, path, header_text = await _read_http_request_full(reader)
    except Exception:  # noqa: BLE001
        writer.close()
        return

    if method == "POST" and path.startswith("/internal/scheduler/manual-run"):
        # Phase 5D: manual run of a job on a MANAGED target through the single
        # remote execution engine. Localhost/private bind only (same trust
        # boundary as the API). The API enforced DROP confirmation; the claim
        # enforces it again in SQL.
        try:
            body = await _read_http_body(reader, header_text)
            payload = json.loads(body or b"{}")
            job_id = int(payload["job_id"])
            attention_id = payload.get("attention_id")
            attention_id = int(attention_id) if attention_id is not None else None
            confirm_drop = bool(payload.get("confirm_drop", False))
            if job_id < 1:
                raise ValueError("job_id")
        except Exception:  # noqa: BLE001
            await _write_http(
                writer, 400,
                {"status": "BAD_REQUEST",
                 "message": "job_id (and optional confirm_drop / attention_id) required"},
                reason="Bad Request")
            return
        result = await scheduler.run_manual_remote(
            job_id, confirm_drop=confirm_drop, attention_id=attention_id)
        await _write_http(writer, 200, result)
        return

    if method == "GET" and path.startswith("/health"):
        await _write_http(
            writer,
            200,
            {"status": "ok", "scheduler_active": scheduler.status.scheduler_active},
        )
        return

    if method == "GET" and path.startswith("/internal/scheduler/status"):
        await _write_http(writer, 200, scheduler.status.as_dict())
        return

    if method == "POST" and path.startswith("/internal/scheduler/refresh"):
        # Signal only — PostgreSQL remains authoritative.
        scheduler.request_refresh()
        await _write_http(
            writer,
            202,
            {
                "accepted": True,
                "message": "Refresh signal accepted; scheduler will reread PostgreSQL.",
            },
            reason="Accepted",
        )
        return

    await _write_http(writer, 404, {"error": "not found"}, reason="Not Found")


async def _run_backend(config: SchedulerConfig) -> int:
    lock = ProcessSingletonLock(config.lock_file)
    if not lock.acquire():
        logger.error(
            "Another scheduler backend already holds the lock file %s",
            config.lock_file,
        )
        return 1

    scheduler = PartitionScheduler(config)
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        scheduler.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signals via KeyboardInterrupt / console events only.
            signal.signal(sig, lambda *_: _signal_handler())

    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, scheduler),
        host=config.bind_host,
        port=config.bind_port,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("Control API listening on %s (localhost/private bind)", sockets)

    scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler-loop")
    try:
        await scheduler_task
    finally:
        server.close()
        await server.wait_closed()
        lock.release()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Realtime partition-job scheduler backend"
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("PARTITION_SCHEDULER_LOG_LEVEL", "INFO"),
        help="Logging level (default INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_scheduler_config()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load scheduler configuration: %s", exc)
        return 2

    if config.bind_host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "Control API bind host is %s — prefer 127.0.0.1 for private use",
            config.bind_host,
        )

    try:
        return asyncio.run(_run_backend(config))
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
