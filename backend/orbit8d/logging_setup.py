"""结构化 JSON 日志：每条带 trace_id（项目 / 导出 ID），同时写 stderr 和滚动日志文件。"""

import contextvars
import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = "orbit8d.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
        payload = {
            "ts": f"{stamp}.{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in (
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(logs_dir / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root.addHandler(handler)


@contextmanager
def trace(trace_id: str) -> Iterator[None]:
    token = trace_id_var.set(trace_id)
    try:
        yield
    finally:
        trace_id_var.reset(token)
