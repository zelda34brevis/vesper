from __future__ import annotations

import logging


def configure_logging(verbose: bool = False) -> None:
    """Configure a simple process-wide logging format for the downloader CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
