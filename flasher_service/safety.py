"""
Device safety checks.

Protects against accidentally flashing the boot medium (SD card) or the
currently running root filesystem's block device.
"""

import logging
import os
import re
import subprocess
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with no shell and return the result."""
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Root / boot device detection
# ---------------------------------------------------------------------------

def get_root_device() -> Optional[str]:
    """
    Return the block device that backs the current root filesystem (e.g.
    /dev/mmcblk0 or /dev/mmcblk0p2).  Uses ``findmnt`` first, falls back to
    parsing /proc/mounts.
    """
    result = _run(["findmnt", "-n", "-o", "SOURCE", "/"])
    if result.returncode == 0:
        src = result.stdout.strip()
        if src:
            return src
    # Fallback: parse /proc/mounts
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    return parts[0]
    except OSError:
        pass
    return None


def get_root_disk(root_device: Optional[str] = None) -> Optional[str]:
    """
    Return the parent whole-disk device for *root_device*.  E.g. if root is
    /dev/mmcblk0p2, return /dev/mmcblk0.
    """
    if root_device is None:
        root_device = get_root_device()
    if root_device is None:
        return None
    # Strip partition suffix  mmcblk0p2 -> mmcblk0  sda1 -> sda
    dev_name = os.path.basename(root_device)
    result = _run(["lsblk", "-no", "PKNAME", root_device])
    if result.returncode == 0 and result.stdout.strip():
        parent = result.stdout.strip().splitlines()[0].strip()
        if parent:
            return f"/dev/{parent}"
    # Heuristic fallback
    m = re.match(r"^(mmcblk\d+)p\d+$", dev_name)
    if m:
        return f"/dev/{m.group(1)}"
    m = re.match(r"^([a-z]+)\d+$", dev_name)
    if m:
        return f"/dev/{m.group(1)}"
    return root_device


def _is_removable(dev_name: str) -> bool:
    """Return True if the kernel marks this device as removable."""
    val = _read_file(f"/sys/block/{dev_name}/removable")
    return val == "1"


def _device_type(dev_name: str) -> Optional[str]:
    """
    Return the MMC device type string (e.g. 'MMC', 'SD') from sysfs, or None.
    """
    return _read_file(f"/sys/block/{dev_name}/device/type")


# ---------------------------------------------------------------------------
# eMMC auto-detection
# ---------------------------------------------------------------------------

def find_emmc_devices() -> List[str]:
    """
    Return a list of whole-disk block device paths that look like eMMC.

    Criteria:
    - Name matches mmcblk*
    - sysfs type is 'MMC' (not 'SD')
    - Not removable
    """
    candidates = []
    try:
        block_devs = os.listdir("/sys/block")
    except OSError:
        return candidates
    for dev in sorted(block_devs):
        if not re.match(r"^mmcblk\d+$", dev):
            continue
        if _is_removable(dev):
            continue
        dtype = _device_type(dev)
        # dtype may be None on older kernels; accept None as possible eMMC
        if dtype is not None and dtype.upper() not in ("MMC",):
            continue
        candidates.append(f"/dev/{dev}")
    return candidates


def auto_detect_target() -> Optional[str]:
    """
    Auto-detect the eMMC user area device, excluding the current root disk.
    Returns the device path or None.
    """
    root_disk = get_root_disk()
    candidates = find_emmc_devices()
    for dev in candidates:
        if root_disk and os.path.realpath(dev) == os.path.realpath(root_disk):
            continue
        return dev
    return None


# ---------------------------------------------------------------------------
# Partition listing / unmounting
# ---------------------------------------------------------------------------

def list_partitions(device: str) -> List[str]:
    """Return a list of partition block devices belonging to *device*."""
    result = _run(["lsblk", "-ln", "-o", "NAME", device])
    if result.returncode != 0:
        return []
    dev_name = os.path.basename(device)
    parts = []
    for line in result.stdout.strip().splitlines():
        name = line.strip()
        if name and name != dev_name:
            parts.append(f"/dev/{name}")
    return parts


def unmount_device_partitions(device: str) -> None:
    """
    Attempt to unmount all partitions of *device*.  Logs warnings on failure
    but does not raise; the caller should treat remaining mounts as an error.
    """
    for part in list_partitions(device):
        result = _run(["umount", part])
        if result.returncode != 0:
            # umount returns 32 if not mounted - that's fine
            logger.debug("umount %s: %s", part, result.stderr.strip())


def get_mountpoints(device: str) -> List[str]:
    """Return a list of active mountpoints for partitions of *device*."""
    partitions = list_partitions(device) + [device]
    mountpoints = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in partitions:
                    mountpoints.append(parts[1])
    except OSError:
        pass
    return mountpoints


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------

def check_target_safety(device: str) -> None:
    """
    Raise ValueError with a descriptive message if *device* must not be
    flashed.

    Checks:
    1. Device path looks sane (starts with /dev/).
    2. Device actually exists.
    3. Device is not the current root device or its parent disk.
    4. Device is not removable media that appears to be the boot SD card.
    """
    if not device.startswith("/dev/"):
        raise ValueError(f"Device path must start with /dev/: {device!r}")

    real_device = os.path.realpath(device)
    if not os.path.exists(real_device):
        raise ValueError(f"Target device does not exist: {device!r}")

    root_device = get_root_device()
    root_disk = get_root_disk(root_device)

    # Resolve for comparison
    def _real(p: Optional[str]) -> Optional[str]:
        return os.path.realpath(p) if p else None

    real_root = _real(root_device)
    real_root_disk = _real(root_disk)

    if real_device == real_root:
        raise ValueError(
            f"Refusing to flash current root device {device!r} "
            f"(root is {root_device!r})"
        )

    if real_root_disk and real_device == real_root_disk:
        raise ValueError(
            f"Refusing to flash parent disk {device!r} of root filesystem "
            f"(root disk is {root_disk!r})"
        )

    # Check if target is removable (SD card); refuse in that case
    dev_name = os.path.basename(real_device)
    if _is_removable(dev_name):
        dtype = _device_type(dev_name)
        if dtype is not None and dtype.upper() == "SD":
            raise ValueError(
                f"Refusing to flash removable SD card {device!r}"
            )
        # Even without explicit SD type, removable + SD boot context is risky
        raise ValueError(
            f"Refusing to flash removable device {device!r} "
            "(appears to be boot media)"
        )


def list_block_devices() -> list:
    """
    Return a simplified list of block devices for the /devices endpoint.
    """
    result = _run(
        ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,RM,MODEL,TRAN"]
    )
    if result.returncode == 0:
        import json
        try:
            return json.loads(result.stdout).get("blockdevices", [])
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def list_uuu_usb_devices(uuu_path: str = "uuu") -> List[dict]:
    """
    Return parsed devices from `uuu -lsusb`.
    """
    try:
        result = _run([uuu_path, "-lsusb"])
    except OSError:
        return []
    if result.returncode != 0:
        return []

    devices: List[dict] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "path" not in line.lower():
            continue

        path_match = re.search(r"path\s+([0-9:./-]+)", line, re.IGNORECASE)
        vidpid_match = re.search(
            r"(?:chip|chip_id)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})",
            line,
            re.IGNORECASE,
        )

        devices.append(
            {
                "path": path_match.group(1) if path_match else None,
                "chip": vidpid_match.group(1).lower() if vidpid_match else None,
                "raw": line,
            }
        )
    return devices
