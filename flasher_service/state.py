"""Flash job state management."""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    FLASHING = "flashing"
    MFG_STAGING = "mfg_staging"
    MFG_FLASHING = "mfg_flashing"
    VERIFYING = "verifying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class FlashStatus:
    phase: Phase = Phase.IDLE
    source_url: Optional[str] = None
    target_device: Optional[str] = None

    # Byte counters
    bytes_downloaded: int = 0       # compressed bytes received from HTTP
    bytes_written: int = 0          # uncompressed bytes written to device

    # Content-Length from HTTP response (may be None)
    content_length: Optional[int] = None

    # Timing
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    # Error / info
    last_error: Optional[str] = None

    # MFG/UUU progress metadata
    mfg_tool: Optional[str] = None
    mfg_step: Optional[str] = None
    mfg_current_step: Optional[int] = None
    mfg_total_steps: Optional[int] = None

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time is not None else time.monotonic()
        return end - self.start_time

    def throughput_bps(self) -> float:
        """Uncompressed bytes written per second."""
        elapsed = self.elapsed()
        if elapsed <= 0:
            return 0.0
        return self.bytes_written / elapsed

    def percent(self) -> Optional[float]:
        """Percent of compressed download complete (if Content-Length known)."""
        if self.content_length and self.content_length > 0:
            return min(100.0, self.bytes_downloaded / self.content_length * 100)
        return None

    def to_dict(self) -> dict:
        d: dict = {
            "phase": self.phase.value,
            "source_url": self.source_url,
            "target_device": self.target_device,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_written": self.bytes_written,
            "content_length": self.content_length,
            "elapsed_seconds": round(self.elapsed(), 2),
            "throughput_bps": round(self.throughput_bps(), 1),
            "last_error": self.last_error,
            "mfg_tool": self.mfg_tool,
            "mfg_step": self.mfg_step,
            "mfg_current_step": self.mfg_current_step,
            "mfg_total_steps": self.mfg_total_steps,
        }
        pct = self.percent()
        if pct is not None:
            d["percent"] = round(pct, 2)
        return d


class FlashManager:
    """Thread-safe container for a single running flash job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = FlashStatus()
        self.cancel_flag = threading.Event()

    def is_busy(self) -> bool:
        with self._lock:
            return self.status.phase not in (
                Phase.IDLE,
                Phase.SUCCESS,
                Phase.FAILED,
                Phase.CANCELLED,
            )

    def start(self, source_url: str, target_device: str) -> None:
        with self._lock:
            self.cancel_flag.clear()
            self.status = FlashStatus(
                phase=Phase.DOWNLOADING,
                source_url=source_url,
                target_device=target_device,
                start_time=time.monotonic(),
            )

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self.status, k, v)

    def finish(self, phase: Phase, error: Optional[str] = None) -> None:
        with self._lock:
            self.status.phase = phase
            self.status.end_time = time.monotonic()
            if error is not None:
                self.status.last_error = error

    def get_status(self) -> dict:
        with self._lock:
            return self.status.to_dict()

    def request_cancel(self) -> bool:
        """Signal the running job to stop. Returns True if a job was active."""
        with self._lock:
            if self.status.phase in (
                Phase.DOWNLOADING,
                Phase.FLASHING,
                Phase.MFG_STAGING,
                Phase.MFG_FLASHING,
                Phase.VERIFYING,
            ):
                self.cancel_flag.set()
                return True
            return False


# Global singleton
flash_manager = FlashManager()
