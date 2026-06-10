"""
Unit tests for flasher-service.

Tests are designed to run without root, real block devices, or hardware.
Hardware-dependent paths are mocked.
"""

import gzip
import hashlib
import io
import json
import lzma
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Config & state tests
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        from flasher_service.config import settings
        self.assertEqual(settings.BIND_PORT, 8080)
        self.assertEqual(settings.CHUNK_SIZE, 4 * 1024 * 1024)

    def test_api_token_none_when_empty_env(self):
        import importlib
        import os
        with patch.dict(os.environ, {"FLASHER_API_TOKEN": ""}, clear=False):
            import flasher_service.config as cfg_mod
            importlib.reload(cfg_mod)
            self.assertIsNone(cfg_mod.settings.API_TOKEN)
            # restore
            importlib.reload(cfg_mod)


class TestFlashState(unittest.TestCase):
    def test_initial_state(self):
        from flasher_service.state import FlashManager, Phase
        mgr = FlashManager()
        self.assertFalse(mgr.is_busy())
        self.assertEqual(mgr.get_status()["phase"], Phase.IDLE.value)

    def test_start_and_finish(self):
        from flasher_service.state import FlashManager, Phase
        mgr = FlashManager()
        mgr.start("http://example.com/img.bin", "/dev/mmcblk1")
        self.assertTrue(mgr.is_busy())
        mgr.finish(Phase.SUCCESS)
        self.assertFalse(mgr.is_busy())
        self.assertEqual(mgr.get_status()["phase"], Phase.SUCCESS.value)

    def test_cancel_request(self):
        from flasher_service.state import FlashManager, Phase
        mgr = FlashManager()
        mgr.start("http://example.com/img.bin", "/dev/mmcblk1")
        result = mgr.request_cancel()
        self.assertTrue(result)
        self.assertTrue(mgr.cancel_flag.is_set())

    def test_cancel_when_idle_returns_false(self):
        from flasher_service.state import FlashManager
        mgr = FlashManager()
        result = mgr.request_cancel()
        self.assertFalse(result)

    def test_percent_without_content_length(self):
        from flasher_service.state import FlashStatus
        s = FlashStatus()
        self.assertIsNone(s.percent())

    def test_percent_with_content_length(self):
        from flasher_service.state import FlashStatus
        s = FlashStatus(content_length=1000, bytes_downloaded=250)
        self.assertAlmostEqual(s.percent(), 25.0)

    def test_throughput_zero_elapsed(self):
        from flasher_service.state import FlashStatus
        s = FlashStatus()
        self.assertEqual(s.throughput_bps(), 0.0)

    def test_elapsed(self):
        from flasher_service.state import FlashStatus
        import time
        s = FlashStatus(start_time=time.monotonic())
        time.sleep(0.05)
        self.assertGreater(s.elapsed(), 0)


# ---------------------------------------------------------------------------
# Safety tests
# ---------------------------------------------------------------------------

class TestSafety(unittest.TestCase):
    def test_rejects_non_dev_path(self):
        from flasher_service.safety import check_target_safety
        with self.assertRaises(ValueError, msg="path must start with /dev/"):
            check_target_safety("/tmp/disk.img")

    def test_rejects_nonexistent_device(self):
        from flasher_service.safety import check_target_safety
        with self.assertRaises(ValueError, msg="device does not exist"):
            check_target_safety("/dev/nonexistent_test_device_xyz")

    @patch("flasher_service.safety.get_root_device", return_value="/dev/mmcblk0p1")
    @patch("flasher_service.safety.get_root_disk", return_value="/dev/mmcblk0")
    @patch("flasher_service.safety._is_removable", return_value=False)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.realpath", side_effect=lambda x: x)
    def test_rejects_root_disk(self, *_):
        from flasher_service.safety import check_target_safety
        with self.assertRaises(ValueError, msg="refuses root disk"):
            check_target_safety("/dev/mmcblk0")

    @patch("flasher_service.safety.get_root_device", return_value="/dev/mmcblk0p1")
    @patch("flasher_service.safety.get_root_disk", return_value="/dev/mmcblk0")
    @patch("flasher_service.safety._is_removable", return_value=True)
    @patch("flasher_service.safety._device_type", return_value="SD")
    @patch("os.path.exists", return_value=True)
    @patch("os.path.realpath", side_effect=lambda x: x)
    def test_rejects_removable_sd(self, *_):
        from flasher_service.safety import check_target_safety
        with self.assertRaises(ValueError, msg="refuses SD card"):
            check_target_safety("/dev/mmcblk1")

    @patch("flasher_service.safety.get_root_device", return_value="/dev/mmcblk0p1")
    @patch("flasher_service.safety.get_root_disk", return_value="/dev/mmcblk0")
    @patch("flasher_service.safety._is_removable", return_value=False)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.realpath", side_effect=lambda x: x)
    def test_allows_safe_emmc(self, *_):
        from flasher_service.safety import check_target_safety
        # Should not raise
        check_target_safety("/dev/mmcblk1")

    def test_get_root_disk_parses_partition(self):
        """
        get_root_disk should strip the partition suffix when lsblk is
        unavailable (heuristic fallback).
        """
        from flasher_service.safety import get_root_disk
        with patch("flasher_service.safety._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = get_root_disk("/dev/mmcblk0p2")
        self.assertEqual(result, "/dev/mmcblk0")

    def test_find_emmc_skips_removable(self):
        from flasher_service.safety import find_emmc_devices
        with patch("os.listdir", return_value=["mmcblk0", "mmcblk1"]):
            with patch("flasher_service.safety._is_removable") as mock_rm:
                with patch("flasher_service.safety._device_type", return_value="MMC"):
                    mock_rm.side_effect = lambda dev: dev == "mmcblk0"
                    result = find_emmc_devices()
        self.assertEqual(result, ["/dev/mmcblk1"])


# ---------------------------------------------------------------------------
# URL validation tests
# ---------------------------------------------------------------------------

class TestUrlValidation(unittest.TestCase):
    def test_rejects_ftp(self):
        from flasher_service.flash import validate_url
        with self.assertRaises(ValueError):
            validate_url("ftp://example.com/img.bin")

    def test_accepts_https(self):
        from flasher_service.flash import validate_url
        # Should not raise
        validate_url("https://example.com/img.bin")

    def test_accepts_http(self):
        from flasher_service.flash import validate_url
        validate_url("http://192.168.1.100/img.bin")

    def test_allowlist_blocks_unknown_host(self):
        from flasher_service.flash import validate_url
        from flasher_service import config
        original = config.settings.ALLOWED_HOSTS
        config.settings.ALLOWED_HOSTS = ["allowed.example.com"]
        try:
            with self.assertRaises(ValueError):
                validate_url("https://notallowed.example.com/img.bin")
        finally:
            config.settings.ALLOWED_HOSTS = original

    def test_allowlist_passes_matching_host(self):
        from flasher_service.flash import validate_url
        from flasher_service import config
        original = config.settings.ALLOWED_HOSTS
        config.settings.ALLOWED_HOSTS = ["example.com"]
        try:
            validate_url("https://example.com/img.bin")
        finally:
            config.settings.ALLOWED_HOSTS = original

    def test_allowlist_cidr(self):
        from flasher_service.flash import validate_url
        from flasher_service import config
        original = config.settings.ALLOWED_HOSTS
        config.settings.ALLOWED_HOSTS = ["192.168.1.0/24"]
        try:
            validate_url("https://192.168.1.50/img.bin")
        finally:
            config.settings.ALLOWED_HOSTS = original


# ---------------------------------------------------------------------------
# Decompressor tests
# ---------------------------------------------------------------------------

class TestDecompressors(unittest.TestCase):
    def _make_gzip(self, data: bytes) -> io.BytesIO:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(data)
        buf.seek(0)
        return buf

    def _make_xz(self, data: bytes) -> io.BytesIO:
        compressed = lzma.compress(data)
        return io.BytesIO(compressed)

    def test_none_passthrough(self):
        from flasher_service.flash import _make_decompressor
        data = b"hello world"
        stream = io.BytesIO(data)
        result = _make_decompressor("none", stream)
        self.assertEqual(result.read(), data)

    def test_gzip_decompress(self):
        from flasher_service.flash import _make_decompressor
        data = b"compressed gzip data test"
        gz_stream = self._make_gzip(data)
        result = _make_decompressor("gzip", gz_stream)
        self.assertEqual(result.read(), data)

    def test_xz_decompress(self):
        from flasher_service.flash import _make_decompressor
        data = b"compressed xz data test"
        xz_stream = self._make_xz(data)
        result = _make_decompressor("xz", xz_stream)
        self.assertEqual(result.read(), data)

    def test_unknown_compression_raises(self):
        from flasher_service.flash import _make_decompressor
        with self.assertRaises(ValueError):
            _make_decompressor("bzip2", io.BytesIO(b""))


# ---------------------------------------------------------------------------
# API tests (with mocks)
# ---------------------------------------------------------------------------

class TestApiHealth(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        self.client = TestClient(app)

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class TestApiStatus(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        from flasher_service.state import flash_manager, FlashStatus, Phase
        flash_manager.status = FlashStatus()  # reset
        self.client = TestClient(app)

    def test_status_idle(self):
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["phase"], "idle")


class TestApiFlashAuth(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        from flasher_service import config
        config.settings.API_TOKEN = "secret-token"
        self.client = TestClient(app)

    def tearDown(self):
        from flasher_service import config
        config.settings.API_TOKEN = None

    def test_no_token_returns_401(self):
        resp = self.client.post(
            "/flash",
            json={"image_url": "https://example.com/img.bin"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = self.client.post(
            "/flash",
            json={"image_url": "https://example.com/img.bin"},
            headers={"Authorization": "******"},
        )
        self.assertEqual(resp.status_code, 401)


class TestApiFlashValidation(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        from flasher_service import config
        from flasher_service.state import flash_manager, FlashStatus
        config.settings.API_TOKEN = None
        flash_manager.status = FlashStatus()
        self.client = TestClient(app)

    def test_invalid_compression_returns_422(self):
        resp = self.client.post(
            "/flash",
            json={"image_url": "https://example.com/img.bin", "compression": "bzip2"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_invalid_sha256_returns_422(self):
        resp = self.client.post(
            "/flash",
            json={
                "image_url": "https://example.com/img.bin",
                "expected_sha256": "notahex",
            },
        )
        self.assertEqual(resp.status_code, 422)

    def test_ftp_url_returns_422(self):
        resp = self.client.post(
            "/flash",
            json={"image_url": "ftp://example.com/img.bin"},
        )
        self.assertEqual(resp.status_code, 422)


@patch("flasher_service.api.auto_detect_target", return_value="/dev/mmcblk1")
@patch("flasher_service.api.run_flash")
class TestApiFlashAccepted(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        from flasher_service import config
        from flasher_service.state import flash_manager, FlashStatus, Phase
        config.settings.API_TOKEN = None
        # Reset to idle
        flash_manager.status = FlashStatus()
        flash_manager.cancel_flag.clear()
        self.client = TestClient(app)

    def test_flash_returns_202(self, mock_run, mock_detect):
        resp = self.client.post(
            "/flash",
            json={"image_url": "https://example.com/img.bin"},
        )
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("target_device", data)

    def test_concurrent_flash_returns_409(self, mock_run, mock_detect):
        from flasher_service.state import flash_manager, Phase
        flash_manager.status.phase = Phase.FLASHING
        resp = self.client.post(
            "/flash",
            json={"image_url": "https://example.com/img.bin"},
        )
        self.assertEqual(resp.status_code, 409)


class TestApiCancel(unittest.TestCase):
    def setUp(self):
        from flasher_service.api import app
        from flasher_service import config
        from flasher_service.state import flash_manager, FlashStatus, Phase
        config.settings.API_TOKEN = None
        flash_manager.status = FlashStatus()
        flash_manager.cancel_flag.clear()
        self.client = TestClient(app)

    def test_cancel_when_idle_returns_409(self):
        resp = self.client.post("/cancel")
        self.assertEqual(resp.status_code, 409)

    def test_cancel_when_busy_returns_200(self):
        from flasher_service.state import flash_manager, Phase
        flash_manager.status.phase = Phase.FLASHING
        resp = self.client.post("/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(flash_manager.cancel_flag.is_set())


# ---------------------------------------------------------------------------
# Flash integration test (no real device)
# ---------------------------------------------------------------------------

class TestFlashIntegration(unittest.TestCase):
    """
    Test the full run_flash() path using a fake file as the 'device' and
    a simple HTTP mock.
    """

    def _make_response_mock(self, data: bytes, compressed: bool = False) -> MagicMock:
        """Create a mock requests.Response that streams *data*."""
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Length": str(len(data))}
        chunk_size = 1024

        def iter_content(chunk_size=chunk_size):
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

        resp.iter_content = iter_content
        return resp

    def _run_flash_to_file(
        self,
        image_data: bytes,
        compression: str = "none",
        expected_sha256: str = None,
        expected_size: int = None,
    ) -> tuple:
        """Run flash to a tmp file and return (manager, tmpfile_path)."""
        import tempfile
        from flasher_service.state import FlashManager
        from flasher_service.flash import run_flash

        # Prepare compressed data if needed
        if compression == "gzip":
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(image_data)
            wire_data = buf.getvalue()
        elif compression == "xz":
            wire_data = lzma.compress(image_data)
        else:
            wire_data = image_data

        mock_resp = self._make_response_mock(wire_data)
        manager = FlashManager()

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmpfile = tf.name

        with patch("flasher_service.flash.requests.get", return_value=mock_resp):
            with patch("flasher_service.flash.check_target_safety"):
                with patch("flasher_service.flash.unmount_device_partitions"):
                    with patch("flasher_service.flash.get_mountpoints", return_value=[]):
                        with patch("flasher_service.flash.subprocess.run"):
                            manager.start("https://example.com/img.bin", tmpfile)
                            run_flash(
                                manager=manager,
                                image_url="https://example.com/img.bin",
                                compression=compression,
                                expected_sha256=expected_sha256,
                                expected_uncompressed_size=expected_size,
                                target_device=tmpfile,
                                reboot_on_success=False,
                            )
        return manager, tmpfile

    def test_flash_uncompressed(self):
        from flasher_service.state import Phase
        data = os.urandom(8192)
        manager, tmpfile = self._run_flash_to_file(data)
        try:
            self.assertEqual(manager.status.phase, Phase.SUCCESS)
            with open(tmpfile, "rb") as f:
                written = f.read()
            self.assertEqual(written, data)
        finally:
            os.unlink(tmpfile)

    def test_flash_gzip(self):
        from flasher_service.state import Phase
        data = os.urandom(8192)
        manager, tmpfile = self._run_flash_to_file(data, compression="gzip")
        try:
            self.assertEqual(manager.status.phase, Phase.SUCCESS)
            with open(tmpfile, "rb") as f:
                written = f.read()
            self.assertEqual(written, data)
        finally:
            os.unlink(tmpfile)

    def test_flash_xz(self):
        from flasher_service.state import Phase
        data = os.urandom(4096)
        manager, tmpfile = self._run_flash_to_file(data, compression="xz")
        try:
            self.assertEqual(manager.status.phase, Phase.SUCCESS)
            with open(tmpfile, "rb") as f:
                written = f.read()
            self.assertEqual(written, data)
        finally:
            os.unlink(tmpfile)

    def test_flash_sha256_match(self):
        from flasher_service.state import Phase
        data = os.urandom(4096)
        sha256 = hashlib.sha256(data).hexdigest()
        manager, tmpfile = self._run_flash_to_file(data, expected_sha256=sha256)
        try:
            self.assertEqual(manager.status.phase, Phase.SUCCESS)
        finally:
            os.unlink(tmpfile)

    def test_flash_sha256_mismatch(self):
        from flasher_service.state import Phase
        data = os.urandom(4096)
        manager, tmpfile = self._run_flash_to_file(
            data, expected_sha256="a" * 64
        )
        try:
            self.assertEqual(manager.status.phase, Phase.FAILED)
            self.assertIn("SHA-256", manager.status.last_error)
        finally:
            os.unlink(tmpfile)

    def test_flash_size_mismatch(self):
        from flasher_service.state import Phase
        data = os.urandom(4096)
        manager, tmpfile = self._run_flash_to_file(
            data, expected_size=len(data) + 100
        )
        try:
            self.assertEqual(manager.status.phase, Phase.FAILED)
            self.assertIn("Size mismatch", manager.status.last_error)
        finally:
            os.unlink(tmpfile)

    def test_flash_cancel(self):
        """Cancel flag set before write starts should produce CANCELLED phase."""
        import tempfile
        from flasher_service.state import FlashManager, Phase
        from flasher_service.flash import run_flash

        data = os.urandom(8192)
        mock_resp = self._make_response_mock(data)
        manager = FlashManager()

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmpfile = tf.name

        # Set cancel before starting
        manager.cancel_flag.set()

        with patch("flasher_service.flash.requests.get", return_value=mock_resp):
            with patch("flasher_service.flash.check_target_safety"):
                with patch("flasher_service.flash.unmount_device_partitions"):
                    with patch("flasher_service.flash.get_mountpoints", return_value=[]):
                        with patch("flasher_service.flash.subprocess.run"):
                            manager.start("https://example.com/img.bin", tmpfile)
                            # Re-set cancel after start (start clears it)
                            manager.cancel_flag.set()
                            run_flash(
                                manager=manager,
                                image_url="https://example.com/img.bin",
                                compression="none",
                                expected_sha256=None,
                                expected_uncompressed_size=None,
                                target_device=tmpfile,
                                reboot_on_success=False,
                            )
        try:
            self.assertEqual(manager.status.phase, Phase.CANCELLED)
        finally:
            os.unlink(tmpfile)

    def test_flash_http_error(self):
        import tempfile
        from flasher_service.state import FlashManager, Phase
        from flasher_service.flash import run_flash
        import requests as req_mod

        manager = FlashManager()

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmpfile = tf.name

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_mod.HTTPError("404 Not Found")

        with patch("flasher_service.flash.requests.get", return_value=mock_resp):
            with patch("flasher_service.flash.check_target_safety"):
                with patch("flasher_service.flash.unmount_device_partitions"):
                    with patch("flasher_service.flash.get_mountpoints", return_value=[]):
                        manager.start("https://example.com/img.bin", tmpfile)
                        run_flash(
                            manager=manager,
                            image_url="https://example.com/img.bin",
                            compression="none",
                            expected_sha256=None,
                            expected_uncompressed_size=None,
                            target_device=tmpfile,
                            reboot_on_success=False,
                        )
        try:
            self.assertEqual(manager.status.phase, Phase.FAILED)
            self.assertIn("HTTP error", manager.status.last_error)
        finally:
            os.unlink(tmpfile)


if __name__ == "__main__":
    unittest.main()
