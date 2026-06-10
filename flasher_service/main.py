"""Entry point for flasher-service."""

import logging
import sys

import uvicorn

from .api import app  # noqa: F401 – register routes
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info(
        "Starting flasher-service on %s:%d",
        settings.BIND_HOST,
        settings.BIND_PORT,
    )
    if settings.API_TOKEN:
        logger.info("****** authentication is ENABLED")
    else:
        logger.warning("****** authentication is DISABLED – set FLASHER_API_TOKEN")

    uvicorn.run(
        "flasher_service.api:app",
        host=settings.BIND_HOST,
        port=settings.BIND_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
