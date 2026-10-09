"""启动入口：加载配置 → 建调度器 → 起多线程 HTTP 服务。

端口被占用时给明确提示并以 1 退出，避免多开两个闸门互相抢端口。
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from typing import Any, Dict, Optional, Tuple

from .config import ConfigError, load_config
from .logger import Logger, create_logger
from .proxy import make_handler_class
from .retry import create_retry_hook
from .scheduler import Scheduler


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """每个连接一个线程：多个 Claude Code 窗口能同时连进来排队。"""

    daemon_threads = True
    # 重启闸门时不被 TIME_WAIT 卡住。
    # Windows 上 SO_REUSEADDR 会允许两个进程绑同一端口，端口占用检测就失效了，所以只在非 Windows 开
    allow_reuse_address = os.name != "nt"
    # 排队中的连接数上限，给得宽一点，真正的限流在调度器里
    request_queue_size = 128

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # 独占端口：第二个闸门启动时直接报端口占用
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # 不调 HTTPServer.server_bind：它里面的 getfqdn 在 Windows 上可能卡好几秒
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request: Any, client_address: Any) -> None:
        # 客户端中途断开会抛一堆 ConnectionReset，不往 stderr 刷栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)):
            return
        logger = getattr(self, "gate_logger", None)
        if logger is not None:
            logger.error("处理请求时异常", {"client": str(client_address), "error": repr(exc)})
        else:
            super().handle_error(request, client_address)


def describe_limits(limits: Any) -> str:
    return "，".join(f"{lim['max']}次/{lim['windowMs']}ms" for lim in limits)


def describe_retry(retry: Dict[str, Any]) -> str:
    """把 retry 配置说成一句人话，启动日志里好看。"""
    if not retry["enabled"] or retry["maxRetries"] < 1:
        return "关闭（429 直接透传）"
    if retry["mode"] == "nextMinute":
        how = f"暂存到下一分钟第 {round(retry['nextMinuteOffsetMs'] / 1000)} 秒"
    else:
        how = f"冷却 {retry['cooldownMs']}ms"
    return f"最多重试 {retry['maxRetries']} 次，{how}"


def build(
    cfg: Optional[Dict[str, Any]] = None,
    logger: Optional[Logger] = None,
    with_retry: bool = True,
) -> Tuple[ThreadedHTTPServer, Scheduler, Logger, Dict[str, Any]]:
    """建好服务但不启动，方便测试里手动控制生命周期。

    with_retry=False 时不挂重试钩子，429 原样透传（老行为，测试用）。
    """
    conf = cfg if cfg is not None else load_config()
    log = logger if logger is not None else create_logger(conf)
    scheduler = Scheduler(conf, logger=log)
    retry_hook = create_retry_hook(conf, logger=log) if (with_retry and "retry" in conf) else None
    handler_cls = make_handler_class(conf, scheduler, log, retry_hook)

    try:
        server = ThreadedHTTPServer((conf["host"], conf["port"]), handler_cls)
    except OSError as err:
        if err.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
            log.error(f"端口 {conf['host']}:{conf['port']} 已被占用。闸门可能已经在跑了，或者换个 GATE_PORT。")
            raise SystemExit(1) from None
        if err.errno in (errno.EACCES, getattr(errno, "WSAEACCES", 10013)):
            log.error(f"没有权限监听 {conf['host']}:{conf['port']}，换一个大于 1024 的端口。")
            raise SystemExit(1) from None
        log.error("启动服务器失败", {"error": str(err)})
        raise SystemExit(1) from None

    server.gate_logger = log  # type: ignore[attr-defined]
    server.retry_hook = retry_hook  # type: ignore[attr-defined]
    # 端口写 0 时取实际分配到的端口，状态接口要显示它
    conf["port"] = server.server_address[1]
    return server, scheduler, log, conf


def start() -> int:
    try:
        cfg = load_config()
    except ConfigError as err:
        sys.stderr.write(f"[cc-rpm-gate] 配置错误: {err}\n")
        return 1

    server, scheduler, logger, cfg = build(cfg)

    logger.info("闸门已启动", {
        "listen": f"http://{cfg['host']}:{cfg['port']}",
        "upstream": cfg["upstream"],
        "limits": describe_limits(cfg["effectiveLimits"]),
        "maxConcurrent": cfg["maxConcurrent"],
        "minGapMs": cfg["minGapMs"],
        "upstreams": list((cfg.get("upstreams") or {}).keys()),
        "retry": describe_retry(cfg["retry"]),
        "runtime": "python",
    })
    logger.info(
        f"把 ANTHROPIC_BASE_URL 指到 http://{cfg['host']}:{cfg['port']} 即可接入；状态查询 /__gate/status"
    )

    closing = threading.Event()

    def shutdown(signum: int, _frame: Any) -> None:
        if closing.is_set():
            return
        closing.set()
        name = signal.Signals(signum).name
        logger.info(f"收到 {name}，正在关闭闸门")
        scheduler.close()
        # shutdown() 必须在别的线程调，否则会和 serve_forever 自锁
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except (ValueError, AttributeError):
        pass  # 某些 Windows 环境没有 SIGTERM

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        scheduler.close()
        server.server_close()
        logger.close()
    return 0


def print_status() -> int:
    """查询正在跑的闸门状态（给 --status 用）。"""
    try:
        cfg = load_config()
    except ConfigError as err:
        sys.stderr.write(f"[cc-rpm-gate] 配置错误: {err}\n")
        return 1
    url = f"http://{cfg['host']}:{cfg['port']}/__gate/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 仅访问本机
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as err:
        sys.stderr.write(f"[cc-rpm-gate] 连不上闸门 {url}: {err}\n")
        return 1
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0
