"""Configuration for flasher-service, loaded from environment variables."""

import os
from typing import List, Optional


class Config:
    # Network
    BIND_HOST: str = os.environ.get("FLASHER_BIND_HOST", "0.0.0.0")
    BIND_PORT: int = int(os.environ.get("FLASHER_BIND_PORT", "8080"))

    # Authentication: set FLASHER_API_TOKEN to require bearer auth.
    # Leave empty to disable authentication (not recommended for production).
    API_TOKEN: Optional[str] = os.environ.get("FLASHER_API_TOKEN") or None

    # Default target device; leave empty to auto-detect eMMC.
    TARGET_DEVICE: Optional[str] = os.environ.get("FLASHER_TARGET_DEVICE") or None

    # Comma-separated allowlist of hostnames or CIDR subnets for image URLs.
    # Leave empty to allow any https/http host.
    ALLOWED_HOSTS: List[str] = [
        h.strip()
        for h in os.environ.get("FLASHER_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    ]

    # Write chunk size in bytes (default 4 MiB).
    CHUNK_SIZE: int = int(os.environ.get("FLASHER_CHUNK_SIZE", str(4 * 1024 * 1024)))

    # HTTP connect/read timeout in seconds.
    HTTP_TIMEOUT: int = int(os.environ.get("FLASHER_HTTP_TIMEOUT", "30"))

    # Path to the NXP Universal Update Utility binary.
    UUU_PATH: str = os.environ.get("FLASHER_UUU_PATH", "uuu")

    # Local working directory used to stage images before invoking uuu.
    MFG_WORK_DIR: str = os.environ.get("FLASHER_MFG_WORK_DIR", "/tmp/flasher-mfg")

    # Optional USB bus/device path used by uuu -m (e.g. "1:10").
    MFG_USB_PATH: Optional[str] = os.environ.get("FLASHER_MFG_USB_PATH") or None

    # Timeout for a full uuu operation in seconds.
    MFG_TIMEOUT: int = int(os.environ.get("FLASHER_MFG_TIMEOUT", "300"))

    # Default uuu -b profile for image flashing.
    MFG_UUU_PROFILE: str = os.environ.get("FLASHER_MFG_UUU_PROFILE", "emmc_all")


settings = Config()
