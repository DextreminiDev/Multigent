"""structlog JSON logging with global secret redaction."""

from __future__ import annotations

import logging
import sys

import structlog

from app.security.redaction import redact, structlog_redact_processor


class _RedactingFilter(logging.Filter):
    """Scrubs stdlib log records (uvicorn, sqlalchemy, httpx, ...) too."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        structlog_redact_processor,  # must run after exc formatting, before rendering
    ]
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,  # allow reconfiguration (tests)
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Never log provider HTTP traffic (headers/bodies may carry keys).
    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
