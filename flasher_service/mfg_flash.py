"""UUU-based flashing flow for NXP manufacturing mode."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Optional

import requests

from .config import settings
from .flash import _make_decompressor, validate_url
from .state import FlashManager, Phase

logger = logging.getLogger(__name__)


def parse_uuu_line(line: str) -> Optional[dict]:
    """Parse UUU progress line and return structured step data."""
    match = re.match(
        r"^\s*(?P<connection>\d+:\d+)\s+(?P<current>\d+)\s*/\s*(?P<total>\d+)\s+\[.*?\]\s*(?P<description>.+?)\s*$",
        line,
    )
    if not match:
        return None

    return {
        "connection_id": match.group("connection"),
        "current_step": int(match.group("current")),
        "total_steps": int(match.group("total")),
        "description": match.group("description"),
    }


def _terminate_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Terminate process gracefully, then force-kill if needed."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _redact_command(command: list[str]) -> list[str]:
    redacted = []
    for arg in command:
        lowered = arg.lower()
        if any(token in lowered for token in ("token", "password", "secret", "key")):
            redacted.append("******")
        else:
            redacted.append(arg)
    return redacted


def run_mfg_flash(
    *,
    manager: FlashManager,
    image_url: str,
    compression: str,
    expected_sha256: Optional[str],
    expected_uncompressed_size: Optional[int],
    uuu_binary: str,
    uuu_profile: str,
    uuu_args: Optional[list[str]],
    mfg_usb_path: Optional[str],
    mfg_work_dir: str,
    mfg_timeout: int,
    reboot_on_success: bool,
) -> None:
    """Stage image locally and flash it through UUU."""
    cancel_flag = manager.cancel_flag
    staged_path: Optional[str] = None
    proc: Optional[subprocess.Popen] = None

    manager.update(
        phase=Phase.MFG_STAGING,
        mfg_tool="uuu",
        mfg_step=None,
        mfg_current_step=None,
        mfg_total_steps=None,
        bytes_downloaded=0,
        bytes_written=0,
    )

    try:
        uuu_exec = shutil.which(uuu_binary)
        if not uuu_exec:
            manager.finish(
                Phase.FAILED,
                error=f"UUU binary not found or not executable: {uuu_binary!r}",
            )
            return

        try:
            validate_url(image_url)
        except ValueError as exc:
            manager.finish(Phase.FAILED, error=str(exc))
            return

        os.makedirs(mfg_work_dir, exist_ok=True)

        try:
            response = requests.get(
                image_url,
                stream=True,
                timeout=settings.HTTP_TIMEOUT,
                headers={"User-Agent": "flasher-service/1.0"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            manager.finish(Phase.FAILED, error=f"HTTP error: {exc}")
            return

        content_length: Optional[int] = None
        cl_header = response.headers.get("Content-Length")
        if cl_header:
            try:
                content_length = int(cl_header)
            except ValueError:
                content_length = None

        manager.update(content_length=content_length)

        if content_length is not None:
            free_bytes = shutil.disk_usage(mfg_work_dir).free
            required = int(content_length * 1.1)
            if free_bytes < required:
                manager.finish(
                    Phase.FAILED,
                    error=(
                        f"Insufficient staging disk space in {mfg_work_dir!r}: "
                        f"required {required} bytes, available {free_bytes} bytes."
                    ),
                )
                return

        staged_path = os.path.join(mfg_work_dir, f"{uuid.uuid4().hex}.img")

        raw_pipe_r_fd, raw_pipe_w_fd = os.pipe()
        feed_error_ref: list[Optional[Exception]] = [None]
        bytes_downloaded_lock = threading.Lock()
        bytes_downloaded_ref = [0]
        hasher_uncompressed = hashlib.sha256()
        bytes_written = 0

        def _feed_pipe() -> None:
            try:
                with os.fdopen(raw_pipe_w_fd, "wb") as pipe_w:
                    for chunk in response.iter_content(chunk_size=settings.CHUNK_SIZE):
                        if cancel_flag.is_set():
                            break
                        if chunk:
                            with bytes_downloaded_lock:
                                bytes_downloaded_ref[0] += len(chunk)
                                downloaded = bytes_downloaded_ref[0]
                            manager.update(bytes_downloaded=downloaded, phase=Phase.MFG_STAGING)
                            pipe_w.write(chunk)
            except Exception as exc:  # noqa: BLE001
                feed_error_ref[0] = exc

        feeder_thread = threading.Thread(target=_feed_pipe, daemon=True)
        feeder_thread.start()

        raw_stream = io.open(raw_pipe_r_fd, "rb", buffering=0)
        write_error: Optional[str] = None

        try:
            decomp_stream = _make_decompressor(compression, raw_stream)
        except ValueError as exc:
            raw_stream.close()
            feeder_thread.join()
            manager.finish(Phase.FAILED, error=str(exc))
            return

        try:
            with open(staged_path, "wb", buffering=0) as staged_file:
                while True:
                    if cancel_flag.is_set():
                        write_error = "Cancelled by user"
                        break
                    try:
                        chunk = decomp_stream.read(settings.CHUNK_SIZE)
                    except EOFError:
                        break
                    except Exception as exc:  # noqa: BLE001
                        write_error = f"Decompression error: {exc}"
                        break
                    if not chunk:
                        break
                    staged_file.write(chunk)
                    hasher_uncompressed.update(chunk)
                    bytes_written += len(chunk)
                    manager.update(bytes_written=bytes_written)
        except OSError as exc:
            write_error = f"Staging write error: {exc}"
        finally:
            try:
                decomp_stream.close()
            except Exception:  # noqa: BLE001
                pass

        feeder_thread.join()

        if feed_error_ref[0] is not None and write_error is None:
            write_error = f"HTTP feed error: {feed_error_ref[0]}"

        if cancel_flag.is_set() or write_error == "Cancelled by user":
            manager.finish(Phase.CANCELLED, error="Cancelled by user")
            return

        if write_error:
            manager.finish(Phase.FAILED, error=write_error)
            return

        if expected_sha256:
            actual = hasher_uncompressed.hexdigest()
            if actual.lower() != expected_sha256.lower():
                manager.finish(
                    Phase.FAILED,
                    error=f"SHA-256 mismatch: expected {expected_sha256.lower()}, got {actual}.",
                )
                return

        if expected_uncompressed_size is not None and bytes_written != expected_uncompressed_size:
            manager.finish(
                Phase.FAILED,
                error=(
                    f"Size mismatch: expected {expected_uncompressed_size} bytes, "
                    f"wrote {bytes_written} bytes."
                ),
            )
            return

        command = [uuu_exec]
        if mfg_usb_path:
            command.extend(["-m", mfg_usb_path])
        if uuu_args:
            command.extend([arg.replace("{image}", staged_path) for arg in uuu_args])
        else:
            command.extend(["-b", uuu_profile, staged_path])

        logger.info("Running UUU command: %s", _redact_command(command))
        manager.update(phase=Phase.MFG_FLASHING, mfg_step="starting")

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        timeout_hit = [False]

        def _watchdog() -> None:
            if mfg_timeout <= 0:
                return
            try:
                proc.wait(timeout=mfg_timeout)
            except subprocess.TimeoutExpired:
                timeout_hit[0] = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return
                time.sleep(1)
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        output_tail: deque[str] = deque(maxlen=20)
        cancelled = False
        assert proc.stdout is not None

        while True:
            if cancel_flag.is_set() and proc.poll() is None:
                cancelled = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(1)
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            line = proc.stdout.readline()
            if line:
                clean_line = line.rstrip()
                output_tail.append(clean_line)
                parsed = parse_uuu_line(clean_line)
                if parsed:
                    manager.update(
                        mfg_step=parsed["description"],
                        mfg_current_step=parsed["current_step"],
                        mfg_total_steps=parsed["total_steps"],
                    )
                continue

            if proc.poll() is not None:
                break
            time.sleep(0.05)

        return_code = proc.wait()

        if cancelled or cancel_flag.is_set():
            manager.finish(Phase.CANCELLED, error="Cancelled by user")
            return

        if timeout_hit[0]:
            manager.finish(Phase.FAILED, error=f"UUU timeout after {mfg_timeout} seconds")
            return

        if return_code != 0:
            tail = "\n".join(output_tail).strip()
            error = f"UUU failed with exit code {return_code}"
            if tail:
                error = f"{error}. Last output:\n{tail}"
            manager.finish(Phase.FAILED, error=error)
            return

        manager.finish(Phase.SUCCESS)

        if reboot_on_success:
            logger.info("Rebooting system as requested")
            time.sleep(2)
            try:
                subprocess.run(
                    ["systemctl", "reboot"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                )
            except Exception:  # noqa: BLE001
                pass

    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if staged_path:
            try:
                os.remove(staged_path)
            except OSError:
                pass
