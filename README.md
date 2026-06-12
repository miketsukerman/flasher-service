# flasher-service

A production-oriented Python 3 service that streams an image from an HTTP URL and flashes it directly to an eMMC (or other block device) on an embedded Linux board booted from an SD card.

- **No local caching** – the image is piped straight from the network to the block device.
- **On-the-fly decompression** – supports gzip, xz and (optionally) zstd.
- **FastAPI HTTP API** – asynchronous, with bearer-token authentication.
- **Safety-first** – refuses to flash the current root device or boot SD card.
- **systemd integration** – ready-made unit file and env config.

---

## Table of Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
  - [Development](#development)
  - [systemd](#systemd)
- [API Reference](#api-reference)
  - [GET /health](#get-health)
  - [GET /status](#get-status)
  - [GET /devices](#get-devices)
  - [POST /flash](#post-flash)
  - [POST /cancel](#post-cancel)
  - [POST /reboot](#post-reboot)
- [curl Examples](#curl-examples)
- [Safety Guarantees](#safety-guarantees)
- [Integrity Checking](#integrity-checking)
- [NXP MFG Tools (UUU)](#nxp-mfg-tools-uuu)
- [Compression Support](#compression-support)
- [Warnings](#warnings)

---

## Architecture

```
flasher_service/
├── __init__.py
├── config.py   – environment-variable configuration
├── state.py    – FlashStatus dataclass + thread-safe FlashManager
├── safety.py   – root/boot device detection and safety gate
├── flash.py    – streaming HTTP→decompress→write pipeline
├── api.py      – FastAPI routes
└── main.py     – entry point (uvicorn)
tests/
└── test_flasher_service.py
flasher-service.service   – systemd unit
flasher-service.env       – example environment config
requirements.txt
```

The flash pipeline uses a POSIX pipe to bridge the HTTP iterator to
Python's gzip/lzma decompressors without ever buffering the whole image:

```
requests.iter_content() ─► os.pipe ─► gzip/lzma/zstd ─► open(/dev/mmcblkN, "wb")
         │                                                       │
    hasher (compressed)                               hasher (uncompressed)
```

---

## Requirements

- Python ≥ 3.10
- Linux (SD-card-booted embedded board)
- `lsblk`, `findmnt`, `blockdev`, `umount` in PATH (standard on Debian/Ubuntu/Yocto)
- `uuu` in PATH for NXP manufacturing-mode USB flashing
- Root or write permission to the target block device

---

## Installation

```bash
# 1. Clone / copy the repository to the target board
git clone https://github.com/miketsukerman/flasher-service /opt/flasher-service
cd /opt/flasher-service

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. (Optional) Enable zstd support
pip install zstandard

# 5. Create config directory and copy example config
sudo mkdir -p /etc/flasher-service
sudo cp flasher-service.env /etc/flasher-service/flasher-service.env
sudo $EDITOR /etc/flasher-service/flasher-service.env

# 6. Install and enable the systemd unit
sudo cp flasher-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flasher-service
```

---

## Configuration

All configuration is done via environment variables (loaded from
`/etc/flasher-service/flasher-service.env` when running under systemd).

| Variable | Default | Description |
|---|---|---|
| `FLASHER_BIND_HOST` | `0.0.0.0` | Address to listen on |
| `FLASHER_BIND_PORT` | `8080` | TCP port |
| `FLASHER_API_TOKEN` | *(empty)* | ****** for auth; leave empty to disable |
| `FLASHER_TARGET_DEVICE` | *(auto)* | Override target block device |
| `FLASHER_ALLOWED_HOSTS` | *(empty)* | Comma-separated hostnames/CIDRs allowed as image sources |
| `FLASHER_CHUNK_SIZE` | `4194304` | Read/write chunk size in bytes (4 MiB) |
| `FLASHER_HTTP_TIMEOUT` | `30` | HTTP connect/read timeout in seconds |
| `FLASHER_UUU_PATH` | `uuu` | Path to UUU binary |
| `FLASHER_MFG_WORK_DIR` | `/tmp/flasher-mfg` | Local staging directory for UUU mode |
| `FLASHER_MFG_USB_PATH` | *(empty)* | Optional UUU USB path (`-m`) |
| `FLASHER_MFG_TIMEOUT` | `300` | UUU operation timeout (seconds) |
| `FLASHER_MFG_UUU_PROFILE` | `emmc_all` | Default UUU `-b` profile |

### Generating a secure token

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Running

### Development

```bash
source venv/bin/activate
python -m flasher_service.main
```

Or directly with uvicorn:

```bash
uvicorn flasher_service.api:app --host 0.0.0.0 --port 8080
```

### systemd

```bash
sudo systemctl start flasher-service
sudo systemctl status flasher-service
journalctl -u flasher-service -f
```

---

## API Reference

All endpoints except `/health` require a bearer token when
`FLASHER_API_TOKEN` is set:

```
Authorization: ******
```

---

### GET /health

Liveness probe. No authentication required.

**Response 200**

```json
{"status": "ok"}
```

---

### GET /status

Returns the current flash job state.

**Response 200**

```json
{
  "phase": "flashing",
  "source_url": "https://fileserver/sdcard.img.gz",
  "target_device": "/dev/mmcblk1",
  "bytes_downloaded": 10485760,
  "bytes_written": 20971520,
  "content_length": 524288000,
  "percent": 2.0,
  "elapsed_seconds": 5.43,
  "throughput_bps": 3860640.1,
  "last_error": null
}
```

**Phases**: `idle` | `downloading` | `flashing` | `mfg_staging` | `mfg_flashing` | `verifying` | `success` | `failed` | `cancelled`

Note: `bytes_downloaded` counts compressed (wire) bytes; `bytes_written` counts uncompressed bytes written to device.

---

### GET /devices

Returns lsblk JSON output of detected block devices.

**Response 200**

```json
{"devices": [...]}
```

---

### POST /flash

Start an asynchronous flash operation.  Returns `202 Accepted` immediately.
Poll `/status` for progress.

**Request body (JSON)**

| Field | Type | Required | Description |
|---|---|---|---|
| `image_url` | string | ✅ | `http://` or `https://` URL of the image |
| `compression` | string | ❌ | `none` (default) \| `gzip` \| `xz` \| `zstd` |
| `expected_sha256` | string | ❌ | 64-char hex SHA-256 of the **uncompressed** image |
| `expected_uncompressed_size` | integer | ❌ | Expected byte count of uncompressed image |
| `target_device` | string | ❌ | Block device override (e.g. `/dev/mmcblk1`) |
| `reboot_on_success` | boolean | ❌ | Reboot after a successful flash (default: false) |
| `flash_method` | string | ❌ | `direct` (default) \| `uuu` |
| `uuu_profile` | string | ❌ | UUU `-b` profile override for `flash_method=uuu` |
| `uuu_args` | array[string] | ❌ | Custom UUU args; supports `{image}` placeholder |
| `mfg_usb_path` | string | ❌ | Optional UUU USB path (`-m`) override |

**Response 202**

```json
{
  "message": "Flash started",
  "target_device": "/dev/mmcblk1",
  "source_url": "https://fileserver/sdcard.img.gz"
}
```

**Error responses**

| Code | Reason |
|---|---|
| `401` | Missing or invalid bearer token |
| `409` | Another flash is already in progress |
| `422` | Invalid request body (bad compression, bad sha256, non-http URL, etc.) |

---

### POST /cancel

Request cancellation of the currently running flash job.

**Response 200**

```json
{"message": "Cancellation requested"}
```

**Response 409** – no active flash operation.

---

### POST /reboot

Immediately reboot the system (rejected while a flash is running).

**Response 200**

```json
{"message": "Rebooting"}
```

---

## curl Examples

```bash
BASE=http://board-ip:8080
TOKEN=your-secret-token

# Health check (no auth needed)
curl $BASE/health

# Current status
curl -H "Authorization: ******" $BASE/status

# List block devices
curl -H "Authorization: ******" $BASE/devices

# Flash a raw image (auto-detect target)
curl -X POST $BASE/flash \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://fileserver/emmc.img"}'

# Flash a gzip-compressed image with SHA-256 verification
curl -X POST $BASE/flash \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://fileserver/emmc.img.gz",
    "compression": "gzip",
    "expected_sha256": "abc123...64hex",
    "expected_uncompressed_size": 4294967296
  }'

# Flash an xz image, reboot on success, explicit target
curl -X POST $BASE/flash \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://fileserver/emmc.img.xz",
    "compression": "xz",
    "target_device": "/dev/mmcblk1",
    "reboot_on_success": true
  }'

# Cancel a running flash
curl -X POST $BASE/cancel \
  -H "Authorization: ******"

# Reboot (after flash completes)
curl -X POST $BASE/reboot \
  -H "Authorization: ******"

# Poll status in a loop
while true; do
  STATUS=$(curl -s -H "Authorization: ******" $BASE/status | python3 -m json.tool)
  echo "$STATUS"
  PHASE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['phase'])")
  case "$PHASE" in
    success|failed|cancelled) break ;;
  esac
  sleep 2
done
```

---

## Safety Guarantees

The service implements multiple layers of protection to prevent accidental
erasure of the running system:

1. **Root device detection** – uses `findmnt /` and `/proc/mounts` to find
   the current root block device and its parent disk.

2. **Removable media rejection** – reads `/sys/block/<dev>/removable` and
   `/sys/block/<dev>/device/type`.  Any device marked removable or typed as
   SD is refused even if explicitly requested.

3. **Path sanity** – all device paths must start with `/dev/`.  The device
   must exist.

4. **Partition unmounting** – all partitions of the target device are
   unmounted before flashing.  If any partition remains mounted, the flash
   is aborted.

5. **eMMC auto-detection** – when no explicit `target_device` is given, the
   service looks for `mmcblk*` devices that are:
   - non-removable
   - typed as `MMC` in sysfs
   - not the current root disk

6. **Post-flash sync** – `fsync()` is called on the device fd, followed by
   `blockdev --rereadpt` and `sync`.

> Note: These guarantees apply to `flash_method=direct`. In `flash_method=uuu`,
> flashing happens through NXP UUU over USB and does not use local `/dev/*`
> target-device safety checks.

---

## Integrity Checking

- SHA-256 is computed over **uncompressed** bytes as they are written.
- If `expected_sha256` is provided, it is compared after all bytes are
  written.
- If `expected_uncompressed_size` is provided, it is compared after all
  bytes are written.

> **⚠️ Warning**: Because the image is streamed directly without local
> caching, a checksum or size mismatch is detected **only after data has
> already been written** to the device.  The device state after such a
> failure is indeterminate.  Always use images from trusted sources and
> verify integrity beforehand where possible.

---

## NXP MFG Tools (UUU)

Set `flash_method` to `uuu` to use NXP Universal Update Utility for USB
manufacturing-mode flashing.

- Images are always staged locally first (default `/tmp/flasher-mfg`)
- Integrity checks (SHA-256 and expected size) are validated before invoking UUU
- Default command shape is:
  - `uuu [-m <usb_path>] -b <profile> <staged_image>`
- Use `uuu_args` to fully override command args; `{image}` is replaced with the
  staged image path.

Example:

```bash
curl -X POST $BASE/flash \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{
    "image_url": "https://fileserver/imx-image.wic",
    "flash_method": "uuu",
    "uuu_profile": "emmc_all",
    "mfg_usb_path": "1:10"
  }'
```

---

## Compression Support

| Format | Library | Notes |
|---|---|---|
| `none` | built-in | Raw image |
| `gzip` | `gzip` (stdlib) | `.img.gz` |
| `xz` | `lzma` (stdlib) | `.img.xz` |
| `zstd` | `zstandard` (optional) | `.img.zst` – install with `pip install zstandard` |

---

## Warnings

- **Run as root** – writing to block devices requires root.
- **Irreversible** – flashing overwrites all data on the target device.
- **No partial-write recovery** – if the connection drops mid-flash, the
  device may be partially written.
- **UUU mode requirements** – `flash_method=uuu` requires `uuu` installed and
  board availability in USB download/manufacturing mode.
- **Authentication** – always set `FLASHER_API_TOKEN` before exposing the
  service on a network.  The `/health` endpoint is unauthenticated by
  design.
