"""用户钩子加载器：用一个小 Python 文件改每次 429 的重试决策，不用改闸门代码。

钩子文件（默认 <root>/gate.hooks.py，没有就等于没配）定义 on_retry：

    def on_retry(ctx):
        if ctx["path"] == "/v1/messages" and ctx["upstream"] == "myprovider":
            return {"maxRetries": 5, "mode": "nextMinute"}

返回 None = 不干预，按配置走；返回 False = 这次不重试，把 429 透传。
改完文件直接保存即可，下一次 429 会按 mtime 自动重载，不用重启闸门。

与 Node 版 src/hooks.js 行为一致（那边是 gate.hooks.js 的 onRetry）。
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from .config import ROOT

# 钩子允许覆盖的字段，以及各自的校验方式
NUM_FIELDS = ("maxRetries", "cooldownMs", "waitMs", "nextMinuteOffsetMs", "maxHoldMs")
BOOL_FIELDS = ("respectRetryAfter",)
MODES = ("cooldown", "nextMinute")


def resolve_hook_path(value: Any, root: Any = ROOT) -> str:
    """把 retry.hook 配置解析成实际文件路径。

    - 空字符串 / False → 不启用
    - 没带扩展名 → 补 .py（Node 版补 .js，两边各认自己的）
    - 相对路径 → 相对仓库根目录
    """
    if value is None or value is False or value == "":
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    ext = Path(raw).suffix.lower()
    # 指定了 JS 钩子：这边不管，交给 Node 版
    if ext in (".js", ".cjs", ".mjs"):
        return ""
    # 写了 .py 就原样用；其余（包括 "gate.hooks" 这种 .hooks 结尾）补 .py，
    # 这样 retry.hook 写 "gate.hooks" 时两套实现各自找 .js / .py。
    # 注意不能用 with_suffix：它会把 "gate.hooks" 变成 "gate.py"。
    if ext != ".py":
        raw += ".py"
    return str(Path(root, raw).resolve())


class UserHook:
    """线程安全：Python 版每个连接一个线程，加载和计数都要加锁。"""

    def __init__(self, file: str, logger: Any = None, required: bool = False) -> None:
        self.file = file or ""
        self.logger = logger
        self.required = bool(required)

        self.fn: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.loaded_from: Optional[str] = None   # 当前 fn 来自哪次 mtime+size
        self.last_error: Optional[str] = None    # 最近一次加载/调用错误
        self.stats = {"calls": 0, "overrides": 0, "forcedPassthrough": 0, "errors": 0, "reloads": 0}
        self._warned: Set[str] = set()           # 同一种非法返回值只提示一次
        self._lock = threading.Lock()            # 计数、状态
        self._reload_lock = threading.Lock()     # 重载（执行用户模块）

    @property
    def enabled(self) -> bool:
        """配了钩子文件就算启用（文件可以晚点再建）。"""
        return self.file != ""

    # ---------- 加载 ----------

    def _sig(self) -> Optional[str]:
        try:
            st = Path(self.file).stat()
            return "%s:%s" % (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload_if_changed(self) -> None:
        """按 mtime 决定是否重新加载。

        加载失败时保留上一个能用的版本：用户存了个半截文件，不该让闸门跟着失灵。
        """
        if not self.enabled:
            return
        sig = self._sig()
        if sig is None:
            if self.fn is not None:
                self._note("钩子文件不见了，继续用上一次加载的版本: %s" % self.file)
            elif self.required and self.last_error is None:
                self._note("找不到钩子文件: %s" % self.file)
            return
        if sig == self.loaded_from:
            return

        try:
            spec = importlib.util.spec_from_file_location("gate_user_hook", self.file)
            if spec is None or spec.loader is None:
                raise ImportError("无法作为 Python 模块加载")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "on_retry", None)
            if not callable(fn):
                raise AttributeError("钩子文件要定义 on_retry 函数（def on_retry(ctx): ...）")
            self.fn = fn
            self.loaded_from = sig
            self.last_error = None
            self._warned.clear()
            self.stats["reloads"] += 1
            if self.logger:
                self.logger.info("已加载重试钩子", {"file": self.file})
        except Exception as err:  # 用户代码，什么都可能抛
            self.stats["errors"] += 1
            self.last_error = str(err)
            # 文件签名记下来，避免同一个坏文件每次 429 都重试加载刷日志
            self.loaded_from = sig
            self._note("加载钩子文件失败，%s: %s" % (
                "继续用上一次加载的版本" if self.fn else "本次忽略钩子", err))

    # ---------- 调用 ----------

    def apply(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """调用钩子，返回 {"overrides": {...}} 或 {"deny": True}；空字典表示不干预。"""
        if not self.enabled:
            return {}
        # 重载会执行用户模块的顶层代码，可能慢，所以单独一把锁。
        # 已经有能用的旧版本时不排队：别的线程正在重载，这次先用旧版本；
        # 一个版本都还没加载时必须等，否则首次加载期间的请求会悄悄跳过钩子。
        if self._reload_lock.acquire(blocking=self.fn is None):
            try:
                self.reload_if_changed()
            finally:
                self._reload_lock.release()
        with self._lock:
            fn = self.fn
            if fn is None:
                return {}
            self.stats["calls"] += 1

        # 调用和校验返回值都包在 try 里：返回一个 dict 子类、__repr__ 会抛的值，
        # 校验阶段一样可能出错，不能漏给 proxy（那边漏了会让并发名额收不回来）
        try:
            return self._coerce(fn(ctx))
        except Exception as err:  # 钩子出错不能影响请求
            msg = _safe_str(err)
            with self._lock:
                self.stats["errors"] += 1
                self.last_error = msg
            self._note("钩子出错，这次按配置走: %s" % msg)
            return {}

    def _coerce(self, out: Any) -> Dict[str, Any]:
        if out is False:
            with self._lock:
                self.stats["forcedPassthrough"] += 1
            return {"deny": True}
        if out is None or out is True:
            return {}
        if not isinstance(out, dict):
            self._warn_once("type", "钩子要返回字典、False 或 None，当前返回 %s，本次忽略"
                            % type(out).__name__)
            return {}
        if out.get("retry") is False:
            with self._lock:
                self.stats["forcedPassthrough"] += 1
            return {"deny": True}

        overrides: Dict[str, Any] = {}
        for key in NUM_FIELDS:
            val = out.get(key)
            if val is None:
                continue
            # 规则与 Node 版一致：只认真整数（bool 不算），不做类型转换；
            # 3.0 这种整数值的 float 在 JS 里就是整数，这里也认
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            ok = isinstance(val, int) and not isinstance(val, bool) and val >= 0
            if ok and (key != "nextMinuteOffsetMs" or val < 60000):
                overrides[key] = val
            else:
                self._warn_once(key, "钩子返回的 %s 不合法（要 ≥ 0 的整数）: %s，忽略这一项"
                                % (key, _safe_repr(val)))
        for key in BOOL_FIELDS:
            val = out.get(key)
            if val is None:
                continue
            if isinstance(val, bool):
                overrides[key] = val
            else:
                self._warn_once(key, "钩子返回的 %s 要是布尔值: %s，忽略这一项" % (key, _safe_repr(val)))
        mode = out.get("mode")
        if mode is not None:
            if isinstance(mode, str) and mode in MODES:
                overrides["mode"] = mode
            else:
                self._warn_once("mode", "钩子返回的 mode 要是 %s 之一: %s，忽略这一项"
                                % ("/".join(MODES), _safe_repr(mode)))

        if overrides:
            with self._lock:
                self.stats["overrides"] += 1
        return {"overrides": overrides}

    # ---------- 其他 ----------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "file": self.file or None,
                "loaded": self.fn is not None,
                "lastError": self.last_error,
                "stats": dict(self.stats),
            }

    def _warn_once(self, key: str, message: str) -> None:
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        self._note(message)

    def _note(self, message: str) -> None:
        if self.logger:
            self.logger.warn(message, {"file": self.file})


def _safe_repr(val: Any) -> str:
    """钩子返回的值可能是任意对象，__repr__ 自己也可能抛，打日志时别跟着出错。"""
    try:
        return repr(val)
    except Exception:
        return "<%s>" % type(val).__name__


def _safe_str(err: BaseException) -> str:
    try:
        return str(err)
    except Exception:
        return type(err).__name__


def create_user_hook(cfg: Dict[str, Any], logger: Any = None, root: Any = ROOT) -> UserHook:
    """按配置建用户钩子。

    没配 retry.hook 时看默认文件 <root>/gate.hooks.py 在不在，在就用。
    """
    raw = (cfg.get("retry") or {}).get("hook")
    explicit = raw is not None and raw != ""
    file = resolve_hook_path(raw if explicit else "gate.hooks.py", root)
    # 没显式配置时，文件不存在就当没这回事
    if not explicit and file and not Path(file).exists():
        return UserHook("", logger=logger)
    hook = UserHook(file, logger=logger, required=explicit)
    hook.reload_if_changed()
    return hook
