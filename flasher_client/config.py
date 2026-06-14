"""
Configuration for flasher-client.

Precedence (highest to lowest):
1. CLI flags passed to the root group (--host, --port, --token, --timeout)
2. Environment variables FLASHER_HOST, FLASHER_PORT, FLASHER_TOKEN, FLASHER_TIMEOUT
3. Hard-coded defaults
"""

import os
from typing import Optional


DEFAULT_HOST: str = "localhost"
DEFAULT_PORT: int = 8080
DEFAULT_TIMEOUT: int = 30


def resolve_host(flag: Optional[str] = None) -> str:
    return flag or os.environ.get("FLASHER_HOST", DEFAULT_HOST)


def resolve_port(flag: Optional[int] = None) -> int:
    if flag is not None:
        return flag
    env = os.environ.get("FLASHER_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_PORT


def resolve_token(flag: Optional[str] = None) -> Optional[str]:
    return flag or os.environ.get("FLASHER_TOKEN") or None


def resolve_timeout(flag: Optional[int] = None) -> int:
    if flag is not None:
        return flag
    env = os.environ.get("FLASHER_TIMEOUT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_TIMEOUT


def build_base_url(host: str, port: int) -> str:
    """Return a base URL without a trailing slash."""
    return f"http://{host}:{port}"
