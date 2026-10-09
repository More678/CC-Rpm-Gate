"""429 重试钩子：上游限流时，决定「闸门自己扛下来重试」还是「把 429 透传给客户端」。

两种暂存模式（mode）：
  - cooldown：  固定冷却 cooldownMs 毫秒后重试（若上游给了更大的 Retry-After 就听它的）
  - nextMinute：暂存到「下一分钟的第 00/01 秒」再重试，对齐按分钟重置的配额

与 Node 版 src/retry.js 行为一致。
注意：nextMinute 必须用墙上时钟（time.time），不能用单调时钟，否则对不上真实的分钟边界。

决策参数的优先级（低 → 高）：gate.config.json 的 retry.* → 请求头
x-gate-max-retries / x-gate-retry-mode → 用户钩子文件（gate/hooks.py）。
钩子能按请求改重试次数和等待方式，改完保存即生效，不用重启闸门。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

MINUTE_MS = 60000
MODES = ("cooldown", "nextMinute")


def next_minute_wait_ms(now_ms: float, offset_ms: float = 0) -> float:
    """距离下一次「分钟边界 + offset_ms」（比如 :01）还有多少毫秒，结果永远 > 0。

    :00.3 收到 429 时，本分钟的 :01 还没到，就等它（多半是上游时钟还没翻页）；
    过了 :01 才等下一分钟的 :01。
    """
    since_minute = now_ms % MINUTE_MS
    if since_minute < offset_ms:
        return offset_ms - since_minute
    return MINUTE_MS - since_minute + offset_ms


def request_overrides(headers: Any) -> Dict[str, Any]:
    """解析单个请求上的覆盖头：x-gate-max-retries / x-gate-retry-mode。"""
    out: Dict[str, Any] = {}
    if headers is None:
        return out
    raw_max = (headers.get("x-gate-max-retries") or "").strip()
    if raw_max:
        try:
            n = int(raw_max)
            # 非法值直接忽略，不能让一个坏头把请求打挂
            if n >= 0:
                out["maxRetries"] = n
        except ValueError:
            pass
    raw_mode = (headers.get("x-gate-retry-mode") or "").strip()
    if raw_mode:
        for mode in MODES:
            if mode.lower() == raw_mode.lower():
                out["mode"] = mode
                break
    return out


def _wall_now_ms() -> float:
    return time.time() * 1000.0


class RetryHook:
    """线程安全：Python 版每个连接一个线程，计数要加锁。"""

    def __init__(
        self,
        retry_cfg: Dict[str, Any],
        logger: Any = None,
        wall_now_ms: Optional[Callable[[], float]] = None,
        user_hook: Any = None,
    ) -> None:
        self.cfg = retry_cfg
        self.logger = logger
        self._wall_now = wall_now_ms or _wall_now_ms
        # 用户钩子（gate/hooks.py 的 UserHook），没配就是 None
        self.user_hook = user_hook
        self._lock = threading.Lock()

        self.stashed = 0              # 当前正在暂存等待的请求数
        self._release_at: List[float] = []  # 暂存中各请求的预计放行时刻（墙上时钟）
        self.stats = {
            "retried": 0,             # 闸门自己发起的重试次数
            "stashed": 0,             # 进入暂存的次数
            "recovered": 0,           # 重试后拿到非限流响应的次数
            "exhausted": 0,           # 重试次数用尽，最终把 429 透传
            "passedThrough": 0,       # 未重试直接透传（含关闭、暂存满、超预算）
            "peakStashed": 0,
            "heldMsTotal": 0,         # 累计暂存等待时长
        }

    # ---------- 对外接口 ----------

    def max_retries_for(self, overrides: Optional[Dict[str, Any]] = None) -> int:
        """这次请求允许重试几回（支持按请求头覆盖）。"""
        n = (overrides or {}).get("maxRetries")
        return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else self.cfg["maxRetries"]

    def mode_for(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        return (overrides or {}).get("mode") or self.cfg["mode"]

    def has_user_hook(self) -> bool:
        """配了用户钩子时，钩子可能把重试次数从 0 调上去，所以请求体得先缓存好。"""
        return bool(self.user_hook is not None and self.user_hook.enabled)

    def decide(
        self,
        status: int,
        attempt: int,
        held_ms: float = 0,
        retry_after_ms: Optional[float] = None,
        overrides: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """拿到上游状态码后决定下一步。

        attempt 是已经发出去的次数（首次 = 1）；held_ms 是累计被暂存了多久。
        优先级（低 → 高）：配置 → 请求头覆盖 → 用户钩子。
        返回 {"action": "retry", "waitMs": ..., "mode": ...} 或 {"action": "passthrough", "reason": ...}
        """
        overrides = overrides or {}
        meta = meta or {}
        if not self.cfg["enabled"]:
            with self._lock:
                return self._pass_locked("disabled")
        if status not in self.cfg["statuses"]:
            return {"action": "passthrough", "reason": "not-retryable"}

        hint = retry_after_ms if (retry_after_ms is not None
                                  and math.isfinite(retry_after_ms)
                                  and retry_after_ms > 0) else 0
        # 本次生效的参数：先配置，再叠请求头
        eff: Dict[str, Any] = {
            "maxRetries": self.max_retries_for(overrides),
            "mode": self.mode_for(overrides),
            "cooldownMs": self.cfg["cooldownMs"],
            "nextMinuteOffsetMs": self.cfg["nextMinuteOffsetMs"],
            "respectRetryAfter": self.cfg["respectRetryAfter"],
            "maxHoldMs": self.cfg["maxHoldMs"],
        }

        # 再叠用户钩子。钩子是用户代码，不在锁里跑，免得它慢就卡住别的线程。
        forced_wait_ms: Optional[int] = None
        if self.user_hook is not None:
            hooked = self.user_hook.apply({
                "status": status,
                "attempt": attempt,
                "heldMs": held_ms,
                "retryAfterMs": hint or None,
                "method": meta.get("method"),
                "path": meta.get("path"),
                "upstream": meta.get("upstream"),
                "window": meta.get("window"),
                "stashed": self.stashed,
                **eff,
            })
            if hooked.get("deny"):
                with self._lock:
                    return self._pass_locked("hook")
            hook_overrides = dict(hooked.get("overrides") or {})
            forced_wait_ms = hook_overrides.pop("waitMs", None)
            eff.update(hook_overrides)

        with self._lock:
            if eff["maxRetries"] < 1:
                return self._pass_locked("retry-disabled")
            if attempt > eff["maxRetries"]:
                return self._pass_locked("max-retries")
            if self.stashed >= self.cfg["maxStashed"]:
                return self._pass_locked("stash-full")

            now = self._wall_now()
            if forced_wait_ms is not None:
                # 钩子直接给了等待时长：照办，不再叠 Retry-After
                wait_ms: float = forced_wait_ms
            elif eff["mode"] == "nextMinute":
                wait_ms = next_minute_wait_ms(now, eff["nextMinuteOffsetMs"])
                # 上游要求等更久：从它指定的时刻再往后对齐到下一个分钟边界
                if hint > wait_ms:
                    wait_ms = next_minute_wait_ms(now + hint, eff["nextMinuteOffsetMs"]) + hint
            elif eff["respectRetryAfter"]:
                wait_ms = max(eff["cooldownMs"], hint)
            else:
                wait_ms = eff["cooldownMs"]

            # 总暂存预算：别让一个请求在闸门里卡到天荒地老
            if eff["maxHoldMs"] > 0 and held_ms + wait_ms > eff["maxHoldMs"]:
                return self._pass_locked("hold-budget")
            return {"action": "retry", "waitMs": wait_ms, "mode": eff["mode"]}

    def enter(self, wait_ms: float) -> float:
        """开始暂存等待，返回预计放行时刻。"""
        with self._lock:
            self.stashed += 1
            self.stats["stashed"] += 1
            if self.stashed > self.stats["peakStashed"]:
                self.stats["peakStashed"] = self.stashed
            at = self._wall_now() + wait_ms
            self._release_at.append(at)
            return at

    def leave(self, release_at: float, waited_ms: float = 0) -> None:
        """暂存等待结束（无论是正常醒来还是客户端断开）。"""
        with self._lock:
            if self.stashed > 0:
                self.stashed -= 1
            try:
                self._release_at.remove(release_at)
            except ValueError:
                pass
            self.stats["heldMsTotal"] += max(0, round(waited_ms))

    def record_retry(self) -> None:
        """真的重新发出去一次。"""
        with self._lock:
            self.stats["retried"] += 1

    def record_recovered(self) -> None:
        """重试之后拿到了非限流响应。"""
        with self._lock:
            self.stats["recovered"] += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._wall_now()
            nxt = min(self._release_at) if self._release_at else None
            return {
                "enabled": self.cfg["enabled"],
                "mode": self.cfg["mode"],
                "maxRetries": self.cfg["maxRetries"],
                "cooldownMs": self.cfg["cooldownMs"],
                "nextMinuteOffsetMs": self.cfg["nextMinuteOffsetMs"],
                "maxHoldMs": self.cfg["maxHoldMs"],
                "maxStashed": self.cfg["maxStashed"],
                "statuses": list(self.cfg["statuses"]),
                "stashed": self.stashed,
                "nextReleaseInMs": 0 if nxt is None else max(0, round(nxt - now)),
                "stats": dict(self.stats),
                "hook": self.user_hook.snapshot() if self.user_hook is not None else {"enabled": False},
            }

    # ---------- 内部 ----------

    def _pass_locked(self, reason: str) -> Dict[str, Any]:
        self.stats["passedThrough"] += 1
        if reason == "max-retries":
            self.stats["exhausted"] += 1
        return {"action": "passthrough", "reason": reason}


_AUTO = object()


def create_retry_hook(cfg: Dict[str, Any], logger: Any = None, user_hook: Any = _AUTO,
                      **kwargs: Any) -> RetryHook:
    """建重试钩子。没显式传 user_hook 时按配置自动加载用户钩子文件。

    传 user_hook=None 可以明确关掉（测试用）。
    """
    if user_hook is _AUTO:
        from .hooks import create_user_hook  # 延迟导入，避免和 config 循环
        user_hook = create_user_hook(cfg, logger=logger)
    return RetryHook(cfg["retry"], logger=logger, user_hook=user_hook, **kwargs)
