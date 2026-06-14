"""
HTTP client for flasher-service.

Wraps every service endpoint in a thin synchronous interface using httpx.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class FlasherClientError(Exception):
    """Raised when the service returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class FlasherClient:
    """Synchronous client for flasher-service."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: int = 30) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"******"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path)
        return self._handle(resp)

    def _post(self, path: str, json: Optional[dict] = None) -> dict[str, Any]:
        resp = self._client.post(path, json=json)
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> dict[str, Any]:
        if resp.is_success:
            try:
                return resp.json()
            except Exception:
                return {}
        # Extract API detail message when available
        detail: str
        try:
            body = resp.json()
            detail = body.get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise FlasherClientError(resp.status_code, detail)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FlasherClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /health"""
        return self._get("/health")

    def status(self) -> dict[str, Any]:
        """GET /status"""
        return self._get("/status")

    def devices(self) -> dict[str, Any]:
        """GET /devices"""
        return self._get("/devices")

    def flash(
        self,
        image_url: str,
        compression: str = "none",
        expected_sha256: Optional[str] = None,
        expected_uncompressed_size: Optional[int] = None,
        target_device: Optional[str] = None,
        reboot_on_success: bool = False,
        flash_method: str = "direct",
        uuu_profile: Optional[str] = None,
        uuu_args: Optional[list[str]] = None,
        mfg_usb_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /flash"""
        payload: dict[str, Any] = {
            "image_url": image_url,
            "compression": compression,
            "reboot_on_success": reboot_on_success,
            "flash_method": flash_method,
        }
        if expected_sha256 is not None:
            payload["expected_sha256"] = expected_sha256
        if expected_uncompressed_size is not None:
            payload["expected_uncompressed_size"] = expected_uncompressed_size
        if target_device is not None:
            payload["target_device"] = target_device
        if uuu_profile is not None:
            payload["uuu_profile"] = uuu_profile
        if uuu_args is not None:
            payload["uuu_args"] = uuu_args
        if mfg_usb_path is not None:
            payload["mfg_usb_path"] = mfg_usb_path
        return self._post("/flash", json=payload)

    def cancel(self) -> dict[str, Any]:
        """POST /cancel"""
        return self._post("/cancel")

    def reboot(self) -> dict[str, Any]:
        """POST /reboot"""
        return self._post("/reboot")
