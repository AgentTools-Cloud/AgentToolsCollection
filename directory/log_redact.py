"""Mask credentials in URL query strings before log records are emitted.

httpx logs every request URL at INFO ("HTTP Request: POST https://...?api_key=...").
Gateway endpoints (Smithery ``api_key``), Etherscan ``apikey`` and similar
sources put secrets in the query string, so those values must never reach
the journal. The filter rewrites the rendered message; it never drops records.
"""
from __future__ import annotations

import logging
import re

_SECRET_PARAM = re.compile(
    r"(?i)([?&;](?:api[_-]?key|apikey|x[_-]?api[_-]?key|key|access[_-]?token|token|"
    r"auth|authorization|secret|client[_-]?secret|password|passwd|sig|signature)=)"
    r"[^&;#\s\"'<>]+"
)
MASK = "***"

# Loggers that render outbound or inbound request URLs.
URL_LOGGERS = ("httpx", "httpcore", "urllib3", "uvicorn.access", "uvicorn.error")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1" + MASK, text)


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


_FILTER = RedactSecretsFilter()


def install() -> None:
    """Attach the filter to URL-logging loggers and to every root handler.

    Logger-level filters cover records created by those loggers; root handler
    filters cover anything else that propagates. Idempotent.
    """
    for name in URL_LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    for handler in logging.getLogger().handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
