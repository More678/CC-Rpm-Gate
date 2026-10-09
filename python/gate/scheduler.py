"""全局排队调度器（线程安全版）。

所有窗口的请求都走这里，按 FIFO 一个个放行。
闸门条件：多条「每 X 毫秒最多 Y 次」滑动窗口 + 并发数 + 最小间隔，再叠加 429 后的全局冷却。

Node 版用事件循环 + 定时器，这里是多线程阻塞模型：
排队的线程挂在一个 Condition 上，只有队首线程有权放行，从而保证先来先得。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional


class GateError(Exception):
    """闸门拒绝了这次请求，带上要回给客户端的状态码。"""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class _Waiter:
    __slots__ = ("ticket", "meta", "enqueued_at", "cancelled", "priority")

    def __init__(self, ticket: int, meta: Dict[str, Any], enqueued_at: float,
                 priority: bool = False) -> None:
        self.ticket = ticket
        self.meta = meta
        self.enqueued_at = enqueued_at
        self.cancelled = False
        self.priority = priority


class Slot:
    """放行名额。请求结束后必须 release()，用 with 语句最稳。"""

    __slots__ = ("ticket", "waited_ms", "_scheduler", "_released")

    def __init__(self, scheduler: "Scheduler", ticket: int, waited_ms: float) -> None:
        self._scheduler = scheduler
        self._released = False
        self.ticket = ticket
        self.waited_ms = waited_ms

    def release(self) -> None:
        # 幂等，避免并发数被重复扣减
        if self._released:
            return
        self._released = True
        self._scheduler._release()

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def _default_now_ms() -> float:
    # 用单调时钟，系统时间被改了也不会算错窗口。
    # 不用 time.monotonic：Python 3.12 及以前在 Windows 上它只有约 15.6ms 精度，perf_counter 同样单调且精度高
    return time.perf_counter() * 1000.0


class Scheduler:
    def __init__(
        self,
        cfg: Dict[str, Any],
        logger: Any = None,
        now_ms: Optional[Callable[[], float]] = None,
    ) -> None:
        self.cfg = cfg
        self.logger = logger
        self._now = now_ms or _default_now_ms
        self._cond = threading.Condition(threading.Lock())

        limits = cfg.get("effectiveLimits") or [
            {"windowMs": cfg["windowMs"], "max": cfg["rpm"]}
        ]
        # 每条限制自带一份放行时间戳队列（窗口越短的放前面，便于日志阅读）
        self._limits: List[Dict[str, Any]] = [
            {"windowMs": int(lim["windowMs"]), "max": int(lim["max"]), "hits": deque()}
            for lim in sorted(limits, key=lambda l: l["windowMs"])
        ]

        self._queue: deque[_Waiter] = deque()
        self._in_flight = 0
        self._last_dispatch_at = 0.0
        self._penalty_until = 0.0
        self.penalty_streak = 0
        self._seq = 0
        self._closed = False

        self.stats = {
            "admitted": 0,
            "rejected": 0,
            "timedOut": 0,
            "penalties": 0,
            "aborted": 0,
        }

    # ---------- 对外接口 ----------

    def acquire(
        self,
        meta: Optional[Dict[str, Any]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_ms: int = 200,
        priority: bool = False,
        timeout_ms: Optional[float] = None,
    ) -> Slot:
        """申请一个放行名额，阻塞直到放行或被拒。

        cancel_check 每隔 poll_ms 被调一次（持锁状态下，必须是非阻塞的轻量检查），
        返回 True 表示客户端已断开，立刻出队不占名额。

        priority=True 表示暂存重试回来的请求：插到普通请求前面，且不受 maxQueue 限制。
        timeout_ms 给暂存重试放宽排队超时（等到下一分钟可能比 queueTimeoutMs 还长）。
        """
        meta = meta or {}
        with self._cond:
            if self._closed:
                raise GateError("闸门已关闭", "GATE_CLOSED", 503)
            if not priority and len(self._queue) >= self.cfg["maxQueue"]:
                self.stats["rejected"] += 1
                raise GateError(
                    f"排队已满（{len(self._queue)}/{self.cfg['maxQueue']}），请稍后重试",
                    "QUEUE_FULL",
                    503,
                )

            self._seq += 1
            item = _Waiter(self._seq, meta, self._now(), priority=priority)
            if priority:
                # 插到所有普通请求前面，但排在已有的优先请求后面（优先组内部仍是 FIFO）
                at = 0
                while at < len(self._queue) and self._queue[at].priority:
                    at += 1
                self._queue.insert(at, item)
                # 队首可能换人了，把原队首叫起来重新判断
                self._cond.notify_all()
            else:
                self._queue.append(item)
            budget = timeout_ms if (timeout_ms is not None and timeout_ms > 0) else self.cfg["queueTimeoutMs"]
            deadline = item.enqueued_at + budget
            self._log(
                "debug",
                "入队",
                {"ticket": item.ticket, "queued": len(self._queue),
                 "inFlight": self._in_flight, "priority": priority, **meta},
            )

            while True:
                if self._closed:
                    self._drop(item)
                    raise GateError("闸门正在关闭", "GATE_CLOSED", 503)

                now = self._now()

                if item.cancelled or (cancel_check is not None and cancel_check()):
                    self._drop(item)
                    self.stats["aborted"] += 1
                    self._log("debug", "排队中断（客户端断开）", {"ticket": item.ticket, **meta})
                    raise GateError("客户端在排队期间断开", "CLIENT_ABORTED", 499)

                if now >= deadline:
                    self._drop(item)
                    self.stats["timedOut"] += 1
                    self._log(
                        "warn",
                        "排队超时",
                        {"ticket": item.ticket, "waitedMs": round(now - item.enqueued_at), **meta},
                    )
                    raise GateError(
                        f"排队超过 {round(budget)}ms 仍未放行",
                        "QUEUE_TIMEOUT",
                        504,
                    )

                delay = math.inf
                # 只有队首有权放行，后面的线程一直等着，保证先来先得
                if self._queue and self._queue[0] is item:
                    delay = self._next_delay_locked(now)
                    if delay == 0:
                        waited = now - item.enqueued_at
                        self._dispatch_locked(item, now)
                        return Slot(self, item.ticket, waited)

                wait_ms = deadline - now
                if math.isfinite(delay):
                    wait_ms = min(wait_ms, delay)
                # 定期醒来：既为了跑 cancel_check，也兜底防止漏收通知
                wait_ms = min(wait_ms, poll_ms if cancel_check is not None else 1000)
                self._cond.wait(max(wait_ms, 1) / 1000.0)

    def cancel(self, ticket: int) -> bool:
        """按票号取消一个还在排队的请求（给外部线程用）。"""
        with self._cond:
            for item in self._queue:
                if item.ticket == ticket:
                    item.cancelled = True
                    self._cond.notify_all()
                    return True
        return False

    def penalize(self, retry_after_ms: Optional[float] = None, exact_ms: Optional[float] = None) -> float:
        """上游返回 429/529 时调用：进入全局冷却，连续触发则指数退避。

        exact_ms 给精确冷却时长：不做指数退避、不受 maxPenaltyMs 削顶。
        429 暂存用它把「等到下一分钟 00/01 秒」原样落到冷却上。
        """
        with self._cond:
            self.penalty_streak += 1
            self.stats["penalties"] += 1

            if exact_ms is not None and math.isfinite(exact_ms):
                wait = max(0.0, exact_ms)
                until = self._now() + wait
                if until > self._penalty_until:
                    self._penalty_until = until
                self._log("warn", "上游限流，按暂存时长冷却",
                          {"waitMs": wait, "streak": self.penalty_streak})
                self._cond.notify_all()
                return wait

            backoff = self.cfg["penaltyMs"] * (2 ** (self.penalty_streak - 1))
            base = min(backoff, self.cfg["maxPenaltyMs"])
            # 上游明确给了 Retry-After 就听它的，但不超过上限
            hint = retry_after_ms if (retry_after_ms is not None and math.isfinite(retry_after_ms)) else 0
            wait = min(max(base, hint), self.cfg["maxPenaltyMs"])
            until = self._now() + wait
            if until > self._penalty_until:
                self._penalty_until = until
            self._log("warn", "上游限流，进入冷却", {"waitMs": wait, "streak": self.penalty_streak})
            self._cond.notify_all()
            return wait

    def on_success(self) -> None:
        """上游正常返回时调用：重置退避计数。"""
        with self._cond:
            self.penalty_streak = 0

    def next_delay_ms(self) -> float:
        """距离下一次可放行还要等多少毫秒；0 表示现在就能放，inf 表示并发打满。"""
        with self._cond:
            return self._next_delay_locked(self._now())

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def snapshot(self) -> Dict[str, Any]:
        with self._cond:
            now = self._now()
            self._trim_locked(now)
            return {
                "queued": len(self._queue),
                "queuedPriority": sum(1 for it in self._queue if it.priority),
                "inFlight": self._in_flight,
                "maxConcurrent": self.cfg["maxConcurrent"],
                "minGapMs": self.cfg["minGapMs"],
                "limits": [
                    {
                        "windowMs": lim["windowMs"],
                        "max": lim["max"],
                        "used": len(lim["hits"]),
                        # 该窗口下一个名额还要等多久
                        "resetInMs": (
                            max(0, round(lim["hits"][0] + lim["windowMs"] - now))
                            if len(lim["hits"]) >= lim["max"]
                            else 0
                        ),
                    }
                    for lim in self._limits
                ],
                "penaltyMsLeft": max(0, round(self._penalty_until - now)),
                "penaltyStreak": self.penalty_streak,
                "stats": dict(self.stats),
            }

    def close(self) -> None:
        """关闭调度器，把还在排队的请求全部唤醒并拒掉。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # ---------- 内部 ----------

    def _next_delay_locked(self, now: float) -> float:
        self._trim_locked(now)

        if self._in_flight >= self.cfg["maxConcurrent"]:
            return math.inf  # 等 release 事件唤醒

        waits = []
        if now < self._penalty_until:
            waits.append(self._penalty_until - now)
        for lim in self._limits:
            if len(lim["hits"]) >= lim["max"]:
                # 最早那次放行滚出窗口时，才腾出一个名额
                waits.append(lim["hits"][0] + lim["windowMs"] - now)
        if self.cfg["minGapMs"] > 0 and self._last_dispatch_at > 0:
            gap = self._last_dispatch_at + self.cfg["minGapMs"] - now
            if gap > 0:
                waits.append(gap)

        if not waits:
            return 0
        return max(0.0, max(waits))

    def _dispatch_locked(self, item: _Waiter, now: float) -> None:
        self._queue.popleft()
        for lim in self._limits:
            lim["hits"].append(now)
        self._last_dispatch_at = now
        self._in_flight += 1
        self.stats["admitted"] += 1
        self._log(
            "debug",
            "放行",
            {
                "ticket": item.ticket,
                "waitedMs": round(now - item.enqueued_at),
                "inFlight": self._in_flight,
                "queued": len(self._queue),
                **item.meta,
            },
        )
        # 队首换人了，叫醒新的队首
        self._cond.notify_all()

    def _release(self) -> None:
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cond.notify_all()

    def _drop(self, item: _Waiter) -> None:
        try:
            self._queue.remove(item)
        except ValueError:
            pass
        self._cond.notify_all()

    def _trim_locked(self, now: float) -> None:
        for lim in self._limits:
            cutoff = now - lim["windowMs"]
            hits = lim["hits"]
            while hits and hits[0] <= cutoff:
                hits.popleft()

    def _log(self, level: str, msg: str, extra: Any = None) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(msg, extra)
