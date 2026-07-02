from __future__ import annotations

import logging


_NOISY_LIBRARIES = ("openai", "httpx", "httpcore", "elastic_transport")


def configure_logging(verbose: bool = False):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
