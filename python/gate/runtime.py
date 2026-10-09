"""进程内跑闸门：起、停、改配置即时生效。

给 UI 用（gate/ui.py），命令行启动走的还是 server.start()。

「改完即时生效」的意思：改限流参数、上游地址、retry.* 不用重启闸门，
客户端（Claude Code / pi）那边的 ANTHROPIC_BASE_URL 也不用动——
监听地址没变，它们照常连 127.0.0.1:17530。
只有 host / port 改了才需要重启（监听地址变了没法原地换），这时 apply() 会说明。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ROOT, ConfigError, load_config
from .hooks import create_user_hook
from .logger import Logger, create_logger
from .proxy import make_handler_class
from .retry import RetryHook
from .scheduler import Scheduler
from .server import ThreadedHTTPServer, build

CONFIG_FILE = ROOT / "gate.config.json"

# 这些键改了必须重启才能生效（监听地址变了，原地换不了）
RESTART_KEYS = ("host", "port")

# 写回 gate.config.json 时保留的键和顺序，跟仓库里那份对齐，
# 免得保存一次就把文件顺序打乱、让人看不出改了什么
SAVE_KEYS = (
    "port", "host", "upstream",
    "rpm", "windowMs", "limits",
    "maxConcurrent", "minGapMs", "maxQueue", "queueTimeoutMs",
    "penaltyMs", "maxPenaltyMs", "bypassPaths",
    "retry", "upstreams",
    "logFile", "logLevel",
)


def config_to_json(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把生效中的配置转回能写进 gate.config.json 的形状。

    limits 用 {"windowMs": ..., "max": ...}（normalize_limits 认这种写法），
    logFile 写回相对仓库根目录的路径，effectiveLimits 是算出来的不写。
    """
    out: Dict[str, Any] = {}
    for key in SAVE_KEYS:
        if key not in cfg:
            continue
        val = cfg[key]
        if key == "limits":
            out[key] = [{"windowMs": int(l["windowMs"]), "max": int(l["max"])} for l in val]
        elif key == "logFile" and val:
            try:
                out[key] = Path(val).relative_to(ROOT).as_posix()
            except ValueError:
                out[key] = str(val)  # 配到仓库外面去了，原样存绝对路径
        elif key == "retry":
            out[key] = dict(val)
        elif key in ("bypassPaths", "upstreams"):
            out[key] = list(val) if isinstance(val, list) else dict(val)
        else:
            out[key] = val
    return out


def save_config(cfg: Dict[str, Any], file: Any = None) -> str:
    """把配置写回 gate.config.json（先写临时文件再替换，中途出错不会留半截文件）。"""
    target = Path(CONFIG_FILE if file is None else file)
    text = json.dumps(config_to_json(cfg), ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return str(target)


class GateRuntime:
    """在当前进程里管一台闸门。

    线程安全：UI 线程调这些方法，闸门自己在后台线程里跑。
    """

    def __init__(self, logger: Optional[Logger] = None) -> None:
        self._lock = threading.RLock()
        self.logger = logger
        self.cfg: Optional[Dict[str, Any]] = None
        self.server: Optional[ThreadedHTTPServer] = None
        self.scheduler: Optional[Scheduler] = None
        self.retry_hook: Optional[RetryHook] = None
        self.thread: Optional[threading.Thread] = None
        self._owns_logger = logger is None

    # ---------- 状态 ----------

    @property
    def running(self) -> bool:
        with self._lock:
            return self.server is not None and self.thread is not None and self.thread.is_alive()

    def listen_url(self) -> str:
        with self._lock:
            if self.cfg is None:
                return ""
            return f"http://{self.cfg['host']}:{self.cfg['port']}"

    def status(self) -> Dict[str, Any]:
        """给 UI 刷新用的实时快照，不用走 HTTP。"""
        with self._lock:
            if not self.running or self.scheduler is None or self.cfg is None:
                return {"running": False}
            snap: Dict[str, Any] = {
                "running": True,
                "listen": self.listen_url(),
                "upstream": self.cfg["upstream"],
                "upstreams": dict(self.cfg.get("upstreams") or {}),
            }
            snap.update(self.scheduler.snapshot())
            snap["retry"] = (self.retry_hook.snapshot() if self.retry_hook is not None
                             else {"enabled": False})
            return snap

    # ---------- 生命周期 ----------

    def start(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """起闸门。已经在跑就原样返回当前配置。"""
        with self._lock:
            if self.running:
                assert self.cfg is not None
                return self.cfg
            conf = dict(cfg) if cfg is not None else load_config()
            if self.logger is None:
                self.logger = create_logger(conf)
                self._owns_logger = True
            # build() 里会按 conf 建调度器、重试钩子和 server（端口占用会抛 SystemExit）
            server, scheduler, log, conf = build(conf, self.logger)
            self.server, self.scheduler, self.logger, self.cfg = server, scheduler, log, conf
            self.retry_hook = getattr(server, "retry_hook", None)
            self.thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            self.thread.start()
            return conf

    def stop(self, timeout: float = 3.0) -> None:
        """停闸门。排队中的请求会收到 503，在途的请求等它自己走完。"""
        with self._lock:
            server, scheduler, thread = self.server, self.scheduler, self.thread
            self.server = self.scheduler = self.retry_hook = self.thread = None
        if scheduler is not None:
            scheduler.close()
        if server is not None:
            # shutdown() 必须在别的线程调，否则会和 serve_forever 自锁
            threading.Thread(target=server.shutdown, daemon=True).start()
        if thread is not None:
            thread.join(timeout=timeout)
        if server is not None:
            server.server_close()

    def close(self) -> None:
        self.stop()
        with self._lock:
            if self._owns_logger and self.logger is not None:
                self.logger.close()
                self.logger = None

    # ---------- 改配置 ----------

    def apply(self, new_cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """把新配置用到正在跑的闸门上。

        返回 (需要重启, 变化说明列表)。host / port 变了就需要重启，
        其余（限流、上游、retry.*、钩子文件）原地生效，客户端不用改 URL 指向。
        """
        with self._lock:
            if self.cfg is None or not self.running:
                # 没在跑：下次 start() 直接用新配置
                self.cfg = dict(new_cfg)
                return False, []

            old = dict(self.cfg)
            changes = describe_changes(old, new_cfg)
            needs_restart = any(old.get(k) != new_cfg.get(k) for k in RESTART_KEYS)

            # 原地更新同一个 dict：proxy 的 handler 和调度器都拿着它的引用，
            # 换 dict 对象的话 keep-alive 连接上的请求还会读到旧值
            live = self.cfg
            for key, val in new_cfg.items():
                if needs_restart and key in RESTART_KEYS:
                    continue  # 监听地址等重启后再换，先别让状态接口显示错的端口
                live[key] = val

            if self.scheduler is not None:
                self.scheduler.reconfigure(live)
            # 日志级别 / 文件：logger 对象被调度器、proxy、钩子共用，原地改它就全都生效
            if self.logger is not None and (old.get("logLevel") != live.get("logLevel")
                                            or old.get("logFile") != live.get("logFile")):
                self.logger.reconfigure(live.get("logLevel", "info"), live.get("logFile", ""))
            if self.retry_hook is not None:
                self.retry_hook.cfg = live["retry"]
                # 钩子文件路径变了：重新建一个（空路径就等于关掉钩子）
                if old.get("retry", {}).get("hook") != live["retry"].get("hook"):
                    self.retry_hook.user_hook = create_user_hook(live, logger=self.logger)
            if self.logger is not None and changes:
                self.logger.info("配置已更新", {"changes": changes,
                                                "needsRestart": needs_restart})
            return needs_restart, changes

    def restart(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """重启闸门（host / port 改了之后用）。"""
        with self._lock:
            conf = dict(cfg) if cfg is not None else (dict(self.cfg) if self.cfg else None)
        self.stop()
        return self.start(conf)


def describe_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """逐项比出差异，给日志和 UI 提示用。"""
    out: List[str] = []
    for key in SAVE_KEYS:
        if key == "retry":
            o, n = old.get("retry") or {}, new.get("retry") or {}
            for rk in sorted(set(o) | set(n)):
                if o.get(rk) != n.get(rk):
                    out.append(f"retry.{rk}: {_fmt(o.get(rk))} → {_fmt(n.get(rk))}")
            continue
        if old.get(key) != new.get(key):
            out.append(f"{key}: {_fmt(old.get(key))} → {_fmt(new.get(key))}")
    return out


def _fmt(val: Any) -> str:
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False, separators=(",", ":"))
    if val is None:
        return "(无)"
    return str(val)


def load_or_error() -> Tuple[Optional[Dict[str, Any]], str]:
    """载配置，失败时把错误信息交给调用方显示（UI 里不能直接退出进程）。"""
    try:
        return load_config(), ""
    except ConfigError as err:
        return None, str(err)
