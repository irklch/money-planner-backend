"""JSON-логи в stdout (Cloud Logging забирает их с VM).

Правило: в логи попадают только технические поля из ALLOWED_EXTRA. Суммы, описания операций,
содержимое выписок, токены, тела запросов и query string не логируются никогда.
"""

import json
import logging
import sys
from datetime import UTC, datetime

ALLOWED_EXTRA = {
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
    "exc_type",
    "event",
    "count",
    "bank",
    "provider",
    "model",
    "reason",
    "elapsed_ms",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ALLOWED_EXTRA:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Access-лог uvicorn пишет query string — отключаем, свой лог в RequestContextMiddleware.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = [handler]
        logging.getLogger(name).propagate = False
    # SQLAlchemy echo никогда не включаем: он логирует параметры запросов.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
