"""cc-rpm-gate 的 Python 实现。

与 Node 版功能对等、共用同一个 gate.config.json：
    python -m gate            # 启动闸门
    python -m gate --ui       # 图形界面（Python 版独有）：改配置即时生效
    python -m gate --status   # 查询正在跑的闸门状态
"""

from .config import (
    load_config,
    normalize_limits,
    effective_limits,
    normalize_retry,
    normalize_upstreams,
    parse_duration,
    ConfigError,
)
from .logger import create_logger
from .retry import RetryHook, create_retry_hook, next_minute_wait_ms
from .scheduler import Scheduler, GateError

__version__ = "1.3.0"

__all__ = [
    "load_config",
    "normalize_limits",
    "effective_limits",
    "normalize_retry",
    "normalize_upstreams",
    "parse_duration",
    "ConfigError",
    "create_logger",
    "RetryHook",
    "create_retry_hook",
    "next_minute_wait_ms",
    "Scheduler",
    "GateError",
    "__version__",
]
