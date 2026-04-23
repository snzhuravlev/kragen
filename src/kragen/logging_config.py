"""Structured logging setup."""

import json
import logging
import sys

import structlog

from kragen.services import log_buffer


def _ring_buffer_processor(
    logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Copy each event as one JSON line into the in-memory ring buffer (Diagnostics UI)."""
    try:
        line = json.dumps(event_dict, default=str, ensure_ascii=False)
        log_buffer.append_line(line)
    except Exception:
        pass
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog and stdlib logging for JSON-friendly output."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper()))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _ring_buffer_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
