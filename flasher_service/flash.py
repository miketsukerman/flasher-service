"""
Core streaming flash logic.

Streams an image URL directly to a block device, optionally decompressing
on the fly.  Never caches the full image to local storage.

WARNING: With no-cache streaming, a checksum failure or size mismatch is
detected ONLY AFTER data has already been written to the device.  The device
state after such a failure is indeterminate.
"""

import gzip
import hashlib
import io
import logging
import lzma
import os
import subprocess
import threading
import time
from typing import Optional

import requests

from .config import settings
from .safety import (
    check_target_safety,
    unmount_device_partitions,
    get_mountpoints,
)
from .state import FlashManager, Phase

logger = logging.getLogger(__name__)

# Optional zstandard support
try:
    import zstandard as zstd  # type: ignore
    _ZSTD_AVAILABLE = True
except ImportError:
    _ZSTD_AVAILABLE = False


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url: str) -> None:
    """
    Raise ValueError if the URL is not a safe http/https URL.
    Optionally checks against FLASHER_ALLOWED_HOSTS.
    """
    from urllib.parse import urlparse
    import ipaddress

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed, got scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")

    allowed = settings.ALLOWED_HOSTS
    if allowed:
        host = parsed.hostname.lower()
        matched = False
        for entry in allowed:
            entry = entry.strip()
            # CIDR check
            try:
                net = ipaddress.ip_network(entry, strict=False)
                try:
                    addr = ipaddress.ip_address(host)
                    if addr in net:
                        matched = True
                        break
                except ValueError:
                    pass
            except ValueError:
                pass
            # Hostname exact / suffix match
            if host == entry.lower() or host.endswith("." + entry.lower()):
                matched = True
                break
        if not matched:
            raise ValueError(
                f"Host {host!r} is not in the allowed hosts list"
            )


# ---------------------------------------------------------------------------
# Decompressor factories
# ---------------------------------------------------------------------------

def _make_decompressor(compression: str, raw_stream):
    """
    Wrap *raw_stream* (a file-like object) in a decompressor.
    Returns a readable file-like object producing uncompressed bytes.
    """
    if compression in ("none", "", None):
        return raw_stream
    if compression == "gzip":
        return gzip.open(raw_stream, "rb")
    if compression == "xz":
        return lzma.open(raw_stream, "rb")
    if compression == "zstd":
        if not _ZSTD_AVAILABLE:
            raise ValueError(
                "zstd decompression requested but 'zstandard' package is not installed"
            )
        dctx = zstd.ZstdDecompressor()
        return dctx.stream_reader(raw_stream)
    raise ValueError(f"Unsupported compression: {compression!r}")


# ---------------------------------------------------------------------------
# Main flash routine
# ---------------------------------------------------------------------------

def run_flash(
    *,
    manager: FlashManager,
    image_url: str,
    compression: str,
    expected_sha256: Optional[str],
    expected_uncompressed_size: Optional[int],
    target_device: str,
    reboot_on_success: bool,
) -> None:
    """
    Execute a flash operation in the calling thread.  Updates *manager* with
    progress and sets the final phase (success / failed / cancelled).

    This function is intended to be run in a background thread.
    """
    cancel_flag = manager.cancel_flag

    # ------------------------------------------------------------------
    # 1. Safety checks
    # ------------------------------------------------------------------
    try:
        check_target_safety(target_device)
    except ValueError as exc:
        manager.finish(Phase.FAILED, error=str(exc))
        return

    # ------------------------------------------------------------------
    # 2. Validate URL
    # ------------------------------------------------------------------
    try:
        validate_url(image_url)
    except ValueError as exc:
        manager.finish(Phase.FAILED, error=str(exc))
        return

    # ------------------------------------------------------------------
    # 3. Unmount partitions
    # ------------------------------------------------------------------
    logger.info("Unmounting partitions of %s", target_device)
    unmount_device_partitions(target_device)
    still_mounted = get_mountpoints(target_device)
    if still_mounted:
        manager.finish(
            Phase.FAILED,
            error=f"Target device has active mounts after unmount attempt: {still_mounted}",
        )
        return

    # ------------------------------------------------------------------
    # 4. Open HTTP stream
    # ------------------------------------------------------------------
    manager.update(phase=Phase.DOWNLOADING)
    logger.info("Opening HTTP stream: %s", image_url)
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
            pass
    manager.update(content_length=content_length)

    # ------------------------------------------------------------------
    # 5. Build pipeline: raw HTTP bytes → decompressor → device
    # ------------------------------------------------------------------

    # We use a pipe so decompressors (gzip.open, lzma.open) can use their
    # standard read() interface while we control feeding from the HTTP
    # iterator in a separate thread.

    # raw_pipe_r is what the decompressor reads from
    # raw_pipe_w is where the HTTP feeder writes to
    raw_pipe_r_fd, raw_pipe_w_fd = os.pipe()

    hasher_compressed = hashlib.sha256()
    bytes_downloaded_lock = threading.Lock()
    bytes_downloaded_ref = [0]
    feed_error_ref: list = [None]

    def _feed_pipe():
        """Read HTTP chunks and write to the write end of the pipe."""
        try:
            with os.fdopen(raw_pipe_w_fd, "wb") as pipe_w:
                for chunk in response.iter_content(chunk_size=settings.CHUNK_SIZE):
                    if cancel_flag.is_set():
                        break
                    if chunk:
                        hasher_compressed.update(chunk)
                        with bytes_downloaded_lock:
                            bytes_downloaded_ref[0] += len(chunk)
                        manager.update(
                            bytes_downloaded=bytes_downloaded_ref[0],
                            phase=Phase.FLASHING,
                        )
                        pipe_w.write(chunk)
        except Exception as exc:  # noqa: BLE001
            feed_error_ref[0] = exc
            # raw_pipe_w_fd is closed when we exit the with block

    feeder_thread = threading.Thread(target=_feed_pipe, daemon=True)
    feeder_thread.start()

    # Open the read end of the pipe as a raw file-like object
    raw_stream = io.open(raw_pipe_r_fd, "rb", buffering=0)

    # Wrap with decompressor
    try:
        decomp_stream = _make_decompressor(compression, raw_stream)
    except ValueError as exc:
        raw_stream.close()
        feeder_thread.join()
        manager.finish(Phase.FAILED, error=str(exc))
        return

    # ------------------------------------------------------------------
    # 6. Write to device
    # ------------------------------------------------------------------
    manager.update(phase=Phase.FLASHING)
    logger.info("Flashing %s → %s", image_url, target_device)

    hasher_uncompressed = hashlib.sha256()
    bytes_written = 0
    write_error: Optional[str] = None

    try:
        with open(target_device, "wb", buffering=0) as dev:
            while True:
                if cancel_flag.is_set():
                    write_error = "Cancelled by user"
                    break
                try:
                    chunk = decomp_stream.read(settings.CHUNK_SIZE)
                except EOFError:
                    # gzip / lzma raise EOFError at end of stream
                    break
                except Exception as exc:  # noqa: BLE001
                    write_error = f"Decompression error: {exc}"
                    break
                if not chunk:
                    break
                hasher_uncompressed.update(chunk)
                dev.write(chunk)
                bytes_written += len(chunk)
                manager.update(bytes_written=bytes_written)

            if write_error is None:
                # Flush to device
                logger.info("Syncing %s", target_device)
                os.fsync(dev.fileno())
    except OSError as exc:
        write_error = f"Write error: {exc}"
    finally:
        try:
            decomp_stream.close()
        except Exception:  # noqa: BLE001
            pass

    feeder_thread.join()

    if feed_error_ref[0] is not None:
        write_error = write_error or f"HTTP feed error: {feed_error_ref[0]}"

    if cancel_flag.is_set() or write_error == "Cancelled by user":
        manager.finish(Phase.CANCELLED, error="Cancelled by user")
        return

    if write_error:
        manager.finish(Phase.FAILED, error=write_error)
        return

    # ------------------------------------------------------------------
    # 7. Verification (post-write)
    # ------------------------------------------------------------------
    manager.update(phase=Phase.VERIFYING)

    if expected_sha256:
        # NOTE: With streaming (no local cache), checksum mismatch is detected
        # AFTER data has already been written to the device.
        actual = hasher_uncompressed.hexdigest()
        if actual.lower() != expected_sha256.lower():
            manager.finish(
                Phase.FAILED,
                error=(
                    f"SHA-256 mismatch: expected {expected_sha256.lower()}, "
                    f"got {actual}. Data may have been partially written."
                ),
            )
            return

    if expected_uncompressed_size is not None:
        if bytes_written != expected_uncompressed_size:
            manager.finish(
                Phase.FAILED,
                error=(
                    f"Size mismatch: expected {expected_uncompressed_size} bytes, "
                    f"wrote {bytes_written} bytes."
                ),
            )
            return

    # ------------------------------------------------------------------
    # 8. Re-read partition table
    # ------------------------------------------------------------------
    try:
        subprocess.run(
            ["blockdev", "--rereadpt", target_device],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not re-read partition table for %s", target_device)

    # Kernel-level sync
    try:
        subprocess.run(["sync"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception:  # noqa: BLE001
        pass

    manager.finish(Phase.SUCCESS)
    logger.info("Flash complete: %s → %s (%d bytes)", image_url, target_device, bytes_written)

    # ------------------------------------------------------------------
    # 9. Optional reboot
    # ------------------------------------------------------------------
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
