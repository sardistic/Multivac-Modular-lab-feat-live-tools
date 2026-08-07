from __future__ import annotations

import logging


_NOISY_LIBRARIES = ("openai", "httpx", "httpcore", "elastic_transport")

# Known-benign library warnings we deliberately don't act on.
_SUPPRESSED_SUBSTRINGS = (
    "PyNaCl is not installed",  # voice support unused
)


class _SuppressKnownWarnings(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in _SUPPRESSED_SUBSTRINGS)


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
        handler.addFilter(_SuppressKnownWarnings())
        root.addHandler(handler)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
