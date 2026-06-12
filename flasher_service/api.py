"""
FastAPI routes for flasher-service.
"""

import logging
import shutil
import threading
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator, model_validator

from .config import settings
from .flash import run_flash
from .mfg_flash import run_mfg_flash
from .safety import auto_detect_target, list_block_devices, list_uuu_usb_devices
from .state import Phase, flash_manager

logger = logging.getLogger(__name__)

app = FastAPI(
    title="flasher-service",
    description="Streams and flashes an image to an eMMC device.",
    version="1.0.0",
)

if shutil.which(settings.UUU_PATH) is None:
    logger.warning(
        "UUU binary not found at startup (%s). UUU flash method requests will fail until installed.",
        settings.UUU_PATH,
    )

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """
    If FLASHER_API_TOKEN is configured, verify the bearer token.
    If no token is configured, allow all requests (useful for closed networks).
    """
    if settings.API_TOKEN is None:
        return  # Auth disabled
    if credentials is None or credentials.credentials != settings.API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class FlashRequest(BaseModel):
    image_url: str
    compression: Optional[str] = "none"
    expected_sha256: Optional[str] = None
    expected_uncompressed_size: Optional[int] = None
    target_device: Optional[str] = None
    reboot_on_success: bool = False
    flash_method: Literal["direct", "uuu"] = "direct"
    uuu_profile: Optional[str] = None
    uuu_args: Optional[list[str]] = None
    mfg_usb_path: Optional[str] = None

    @field_validator("compression")
    @classmethod
    def validate_compression(cls, v: Optional[str]) -> str:
        allowed = {"none", "gzip", "xz", "zstd", None, ""}
        if v not in allowed:
            raise ValueError(f"compression must be one of: none, gzip, xz, zstd")
        return v or "none"

    @field_validator("image_url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("image_url must start with http:// or https://")
        return v

    @field_validator("expected_sha256")
    @classmethod
    def validate_sha256(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            import re
            if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
                raise ValueError("expected_sha256 must be a 64-character hex string")
        return v

    @model_validator(mode="after")
    def validate_uuu_fields(self):
        if self.flash_method == "uuu":
            if self.uuu_args and self.uuu_profile:
                raise ValueError("uuu_args and uuu_profile are mutually exclusive")
            if self.uuu_args is not None and len(self.uuu_args) == 0:
                raise ValueError("uuu_args must not be an empty list")
        return self


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """Simple liveness check."""
    return {"status": "ok"}


@app.get("/status", tags=["flash"])
def get_status(_: None = Depends(_require_auth)):
    """Return the current flash job status."""
    return flash_manager.get_status()


@app.get("/devices", tags=["system"])
def get_devices(_: None = Depends(_require_auth)):
    """List block devices detected by lsblk."""
    payload = {"devices": list_block_devices()}
    payload["nxp_usb_devices"] = list_uuu_usb_devices(settings.UUU_PATH)
    return payload


@app.post("/flash", status_code=status.HTTP_202_ACCEPTED, tags=["flash"])
def start_flash(req: FlashRequest, _: None = Depends(_require_auth)):
    """
    Start a flash operation.  Returns 202 Accepted immediately; poll /status.

    POST body (JSON):
    - image_url (required): http/https URL of the image.
    - compression: none | gzip | xz | zstd  (default: none)
    - expected_sha256: optional 64-char hex SHA-256 of *uncompressed* image.
    - expected_uncompressed_size: optional byte count of uncompressed image.
    - target_device: optional override (e.g. /dev/mmcblk1).
    - reboot_on_success: if true, reboot the system after a successful flash.

    WARNING: With streaming (no local cache), a checksum or size mismatch is
    reported ONLY AFTER data has already been written to the target device.
    """
    if flash_manager.is_busy():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A flash operation is already in progress",
        )

    if req.flash_method == "uuu":
        if shutil.which(settings.UUU_PATH) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"flash_method='uuu' requested but UUU binary was not found: "
                    f"{settings.UUU_PATH!r}"
                ),
            )

        mfg_usb_path = req.mfg_usb_path or settings.MFG_USB_PATH
        target = f"uuu:{mfg_usb_path or 'auto'}"
        flash_manager.start(source_url=req.image_url, target_device=target)

        thread = threading.Thread(
            target=run_mfg_flash,
            kwargs=dict(
                manager=flash_manager,
                image_url=req.image_url,
                compression=req.compression or "none",
                expected_sha256=req.expected_sha256,
                expected_uncompressed_size=req.expected_uncompressed_size,
                uuu_binary=settings.UUU_PATH,
                uuu_profile=req.uuu_profile or settings.MFG_UUU_PROFILE,
                uuu_args=req.uuu_args,
                mfg_usb_path=mfg_usb_path,
                mfg_work_dir=settings.MFG_WORK_DIR,
                mfg_timeout=settings.MFG_TIMEOUT,
                reboot_on_success=req.reboot_on_success,
            ),
            daemon=True,
            name="flasher-worker-uuu",
        )
    else:
        # Resolve target device
        target = req.target_device or settings.TARGET_DEVICE
        if target is None:
            target = auto_detect_target()
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Could not auto-detect an eMMC device.  "
                    "Set target_device in the request or FLASHER_TARGET_DEVICE env var."
                ),
            )

        flash_manager.start(source_url=req.image_url, target_device=target)

        thread = threading.Thread(
            target=run_flash,
            kwargs=dict(
                manager=flash_manager,
                image_url=req.image_url,
                compression=req.compression or "none",
                expected_sha256=req.expected_sha256,
                expected_uncompressed_size=req.expected_uncompressed_size,
                target_device=target,
                reboot_on_success=req.reboot_on_success,
            ),
            daemon=True,
            name="flasher-worker",
        )
    thread.start()

    return {
        "message": "Flash started",
        "target_device": target,
        "source_url": req.image_url,
    }


@app.post("/cancel", tags=["flash"])
def cancel_flash(_: None = Depends(_require_auth)):
    """Request cancellation of the running flash job."""
    cancelled = flash_manager.request_cancel()
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active flash operation to cancel",
        )
    return {"message": "Cancellation requested"}


@app.post("/reboot", tags=["system"])
def reboot(_: None = Depends(_require_auth)):
    """Reboot the system immediately."""
    import subprocess

    if flash_manager.is_busy():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot reboot while a flash operation is in progress",
        )
    try:
        subprocess.run(
            ["systemctl", "reboot"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reboot failed: {exc}",
        ) from exc
    return {"message": "Rebooting"}
