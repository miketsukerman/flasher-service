"""
Unit tests for flasher-client CLI and HTTP client.

All network calls are mocked with respx so no real service is needed.
"""

from __future__ import annotations

import json
import unittest

import httpx
import respx
from click.testing import CliRunner

from flasher_client.cli import cli
from flasher_client.client import FlasherClient, FlasherClientError
from flasher_client.config import (
    build_base_url,
    resolve_host,
    resolve_port,
    resolve_timeout,
    resolve_token,
)


BASE_URL = "http://localhost:8080"


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(resolve_host(None), "localhost")
        self.assertEqual(resolve_port(None), 8080)
        self.assertEqual(resolve_timeout(None), 30)
        self.assertIsNone(resolve_token(None))

    def test_flag_overrides(self):
        self.assertEqual(resolve_host("board"), "board")
        self.assertEqual(resolve_port(9090), 9090)
        self.assertEqual(resolve_timeout(60), 60)
        self.assertEqual(resolve_token("mytoken"), "mytoken")

    def test_env_overrides(self, monkeypatch=None):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {
            "FLASHER_HOST": "board.local",
            "FLASHER_PORT": "9999",
            "FLASHER_TOKEN": "envtoken",
            "FLASHER_TIMEOUT": "45",
        }):
            self.assertEqual(resolve_host(None), "board.local")
            self.assertEqual(resolve_port(None), 9999)
            self.assertEqual(resolve_token(None), "envtoken")
            self.assertEqual(resolve_timeout(None), 45)

    def test_build_base_url(self):
        self.assertEqual(build_base_url("board", 8080), "http://board:8080")


# ---------------------------------------------------------------------------
# FlasherClient unit tests
# ---------------------------------------------------------------------------

class TestFlasherClient(unittest.TestCase):

    @respx.mock
    def test_health_ok(self):
        respx.get(f"{BASE_URL}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        with FlasherClient(BASE_URL) as c:
            data = c.health()
        self.assertEqual(data["status"], "ok")

    @respx.mock
    def test_status_ok(self):
        payload = {"phase": "idle", "bytes_written": 0}
        respx.get(f"{BASE_URL}/status").mock(return_value=httpx.Response(200, json=payload))
        with FlasherClient(BASE_URL) as c:
            data = c.status()
        self.assertEqual(data["phase"], "idle")

    @respx.mock
    def test_devices_ok(self):
        payload = {"devices": [{"name": "mmcblk1"}], "nxp_usb_devices": []}
        respx.get(f"{BASE_URL}/devices").mock(return_value=httpx.Response(200, json=payload))
        with FlasherClient(BASE_URL) as c:
            data = c.devices()
        self.assertEqual(len(data["devices"]), 1)

    @respx.mock
    def test_flash_ok(self):
        payload = {"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img"}
        respx.post(f"{BASE_URL}/flash").mock(return_value=httpx.Response(202, json=payload))
        with FlasherClient(BASE_URL) as c:
            data = c.flash("http://f/img")
        self.assertEqual(data["message"], "Flash started")

    @respx.mock
    def test_flash_builds_correct_payload(self):
        captured: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(202, json={"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img.gz"})

        respx.post(f"{BASE_URL}/flash").mock(side_effect=_handler)
        with FlasherClient(BASE_URL) as c:
            c.flash(
                "http://f/img.gz",
                compression="gzip",
                expected_sha256="a" * 64,
                expected_uncompressed_size=1024,
                target_device="/dev/mmcblk1",
                reboot_on_success=True,
            )
        body = captured[0]
        self.assertEqual(body["compression"], "gzip")
        self.assertEqual(body["expected_sha256"], "a" * 64)
        self.assertEqual(body["expected_uncompressed_size"], 1024)
        self.assertEqual(body["target_device"], "/dev/mmcblk1")
        self.assertTrue(body["reboot_on_success"])

    @respx.mock
    def test_cancel_ok(self):
        respx.post(f"{BASE_URL}/cancel").mock(return_value=httpx.Response(200, json={"message": "Cancellation requested"}))
        with FlasherClient(BASE_URL) as c:
            data = c.cancel()
        self.assertEqual(data["message"], "Cancellation requested")

    @respx.mock
    def test_reboot_ok(self):
        respx.post(f"{BASE_URL}/reboot").mock(return_value=httpx.Response(200, json={"message": "Rebooting"}))
        with FlasherClient(BASE_URL) as c:
            data = c.reboot()
        self.assertEqual(data["message"], "Rebooting")

    @respx.mock
    def test_raises_on_401(self):
        respx.get(f"{BASE_URL}/status").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid or missing bearer token"})
        )
        with FlasherClient(BASE_URL) as c:
            with self.assertRaises(FlasherClientError) as cm:
                c.status()
        self.assertEqual(cm.exception.status_code, 401)
        self.assertIn("bearer token", cm.exception.detail)

    @respx.mock
    def test_raises_on_409(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(409, json={"detail": "A flash operation is already in progress"})
        )
        with FlasherClient(BASE_URL) as c:
            with self.assertRaises(FlasherClientError) as cm:
                c.flash("http://f/img")
        self.assertEqual(cm.exception.status_code, 409)

    @respx.mock
    def test_raises_on_422(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(422, json={"detail": "Invalid compression"})
        )
        with FlasherClient(BASE_URL) as c:
            with self.assertRaises(FlasherClientError) as cm:
                c.flash("http://f/img", compression="bad")
        self.assertEqual(cm.exception.status_code, 422)

    @respx.mock
    def test_bearer_token_sent(self):
        captured_headers: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(dict(request.headers))
            return httpx.Response(200, json={"status": "ok"})

        respx.get(f"{BASE_URL}/health").mock(side_effect=_handler)
        with FlasherClient(BASE_URL, token="secret") as c:
            c.health()
        self.assertIn("authorization", captured_headers[0])
        self.assertEqual(captured_headers[0]["authorization"], "******")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):
    def _run(self, args: list[str]) -> object:
        runner = CliRunner()
        return runner.invoke(cli, args, obj={}, catch_exceptions=False)

    @respx.mock
    def test_health_ok(self):
        respx.get(f"{BASE_URL}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        result = self._run(["health"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("healthy", result.output)

    @respx.mock
    def test_health_json_flag(self):
        respx.get(f"{BASE_URL}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        result = self._run(["--json", "health"])
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "ok")

    @respx.mock
    def test_health_quiet_flag(self):
        respx.get(f"{BASE_URL}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        result = self._run(["-q", "health"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output.strip(), "")

    @respx.mock
    def test_status_cmd(self):
        respx.get(f"{BASE_URL}/status").mock(
            return_value=httpx.Response(200, json={"phase": "idle", "bytes_written": 0, "last_error": None})
        )
        result = self._run(["status"])
        self.assertEqual(result.exit_code, 0)

    @respx.mock
    def test_status_watch_exits_on_success(self):
        responses = iter([
            httpx.Response(200, json={"phase": "flashing", "bytes_written": 100, "last_error": None}),
            httpx.Response(200, json={"phase": "success", "bytes_written": 1000, "last_error": None}),
        ])
        respx.get(f"{BASE_URL}/status").mock(side_effect=lambda r: next(responses))
        result = self._run(["status", "--watch", "--interval", "0"])
        self.assertEqual(result.exit_code, 0)

    @respx.mock
    def test_status_watch_exits_1_on_failed(self):
        respx.get(f"{BASE_URL}/status").mock(
            return_value=httpx.Response(200, json={"phase": "failed", "bytes_written": 0, "last_error": "oops"})
        )
        result = self._run(["-q", "status", "--watch", "--interval", "0"])
        self.assertEqual(result.exit_code, 1)

    @respx.mock
    def test_devices_cmd(self):
        respx.get(f"{BASE_URL}/devices").mock(
            return_value=httpx.Response(200, json={"devices": [], "nxp_usb_devices": []})
        )
        result = self._run(["devices"])
        self.assertEqual(result.exit_code, 0)

    @respx.mock
    def test_flash_cmd_basic(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(202, json={"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img"})
        )
        result = self._run(["flash", "http://f/img"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Flash started", result.output)

    @respx.mock
    def test_flash_cmd_auto_compression_gzip(self):
        captured: list[dict] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(202, json={"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img.gz"})

        respx.post(f"{BASE_URL}/flash").mock(side_effect=_handler)
        result = self._run(["flash", "http://f/img.gz"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(captured[0]["compression"], "gzip")

    @respx.mock
    def test_flash_cmd_wait_success(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(202, json={"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img"})
        )
        respx.get(f"{BASE_URL}/status").mock(
            return_value=httpx.Response(200, json={"phase": "success", "bytes_written": 500, "last_error": None})
        )
        result = self._run(["flash", "http://f/img", "--wait", "--interval", "0"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("successfully", result.output)

    @respx.mock
    def test_flash_cmd_wait_failed(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(202, json={"message": "Flash started", "target_device": "/dev/mmcblk1", "source_url": "http://f/img"})
        )
        respx.get(f"{BASE_URL}/status").mock(
            return_value=httpx.Response(200, json={"phase": "failed", "bytes_written": 0, "last_error": "write error"})
        )
        result = self._run(["-q", "flash", "http://f/img", "--wait", "--interval", "0"])
        self.assertEqual(result.exit_code, 1)

    @respx.mock
    def test_flash_http_error_exits_2(self):
        respx.post(f"{BASE_URL}/flash").mock(
            return_value=httpx.Response(401, json={"detail": "Unauthorized"})
        )
        result = self._run(["-q", "flash", "http://f/img"])
        self.assertEqual(result.exit_code, 2)

    @respx.mock
    def test_cancel_cmd(self):
        respx.post(f"{BASE_URL}/cancel").mock(
            return_value=httpx.Response(200, json={"message": "Cancellation requested"})
        )
        result = self._run(["cancel"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Cancellation", result.output)

    @respx.mock
    def test_cancel_conflict(self):
        respx.post(f"{BASE_URL}/cancel").mock(
            return_value=httpx.Response(409, json={"detail": "No active flash operation to cancel"})
        )
        result = self._run(["-q", "cancel"])
        self.assertEqual(result.exit_code, 2)

    @respx.mock
    def test_reboot_cmd_with_yes(self):
        respx.post(f"{BASE_URL}/reboot").mock(
            return_value=httpx.Response(200, json={"message": "Rebooting"})
        )
        result = self._run(["reboot", "--yes"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Rebooting", result.output)

    @respx.mock
    def test_custom_host_port(self):
        respx.get("http://board.local:9090/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        result = self._run(["--host", "board.local", "--port", "9090", "health"])
        self.assertEqual(result.exit_code, 0)

    @respx.mock
    def test_connection_error_exits_2(self):
        respx.get(f"{BASE_URL}/health").mock(side_effect=httpx.ConnectError("refused"))
        result = self._run(["-q", "health"])
        self.assertEqual(result.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
