"""极简日志：按级别过滤，同时写 stdout 和可选的日志文件。

多线程并发写，所以加一把锁，避免两条日志串行到同一行。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}


class Logger:
    def __init__(self, log_level: str = "info", log_file: str = "") -> None:
        self._threshold = LEVELS.get(log_level, LEVELS["info"])
        self._lock = threading.Lock()
        self._stream: Optional[TextIO] = None

        if log_file and self._threshold < LEVELS["silent"]:
            try:
                path = Path(log_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._stream = path.open("a", encoding="utf-8")
            except OSError as err:
                sys.stdout.write(f"[gate] 无法打开日志文件 {log_file}: {err}\n")
                self._stream = None

    def _write(self, level: str, msg: str, extra: Any = None) -> None:
        if LEVELS[level] < self._threshold:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        tail = "" if extra is None else f" {_safe_json(extra)}"
        line = f"{ts} [{level}] {msg}{tail}\n"
        with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()
            if self._stream is not None:
                try:
                    self._stream.write(line)
                    self._stream.flush()
                except OSError as err:
                    # 日志写失败不能把闸门带崩，降级成只写 stdout
                    sys.stdout.write(f"[gate] 日志文件写入失败，转为仅输出到控制台: {err}\n")
                    self._stream = None

    def debug(self, msg: str, extra: Any = None) -> None:
        self._write("debug", msg, extra)

    def info(self, msg: str, extra: Any = None) -> None:
        self._write("info", msg, extra)

    def warn(self, msg: str, extra: Any = None) -> None:
        self._write("warn", msg, extra)

    def error(self, msg: str, extra: Any = None) -> None:
        self._write("error", msg, extra)

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


class NullLogger(Logger):
    """测试用：什么都不输出。"""

    def __init__(self) -> None:  # 不调用父类，避免开文件
        self._threshold = LEVELS["silent"]
        self._lock = threading.Lock()
        self._stream = None

    def _write(self, level: str, msg: str, extra: Any = None) -> None:
        return


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def create_logger(cfg: Optional[dict] = None, **kwargs: Any) -> Logger:
    """从配置字典建日志器，也支持直接传 log_level / log_file。"""
    merged = dict(cfg or {})
    merged.update(kwargs)
    return Logger(
        log_level=merged.get("logLevel", merged.get("log_level", "info")),
        log_file=merged.get("logFile", merged.get("log_file", "")),
    )
