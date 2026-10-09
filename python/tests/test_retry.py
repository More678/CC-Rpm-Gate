"""429 重试钩子单元测试（与 Node 版 test/retry.test.js 对应）。

覆盖：两种暂存模式、请求头覆盖、预算/上限、配置归一化、调度器插队。
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Dict, List, Optional

from gate.config import (
    DEFAULT_RETRY,
    ConfigError,
    load_config,
    normalize_retry,
    normalize_upstreams,
)
from gate.logger import NullLogger
from gate.retry import RetryHook, next_minute_wait_ms, request_overrides
from gate.scheduler import GateError, Scheduler


def hook(wall_now: Any = None, **overrides: Any) -> RetryHook:
    cfg = {**DEFAULT_RETRY, **overrides}
    return RetryHook(cfg, logger=NullLogger(), wall_now_ms=wall_now or (lambda: 0.0))


class _Headers(dict):
    """够用的假请求头容器：Python 版读的是 .get()。"""


class TestNextMinute(unittest.TestCase):
    def test_alignment(self) -> None:
        """对齐到下一个 :00 / :01。"""
        self.assertEqual(next_minute_wait_ms(30000, 0), 30000)
        self.assertEqual(next_minute_wait_ms(30000, 1000), 31000)
        self.assertEqual(next_minute_wait_ms(59500, 1000), 1500)
        self.assertEqual(next_minute_wait_ms(300, 1000), 700, ":00.3 → 本分钟的 :01 还没到")
        self.assertEqual(next_minute_wait_ms(1000, 1000), 60000, "正好 :01 不返回 0")
        self.assertEqual(next_minute_wait_ms(0, 0), 60000)
        self.assertEqual(next_minute_wait_ms(120000 + 45000, 1000), 16000)


class TestDecide(unittest.TestCase):
    def test_cooldown_mode(self) -> None:
        """固定等 cooldownMs，Retry-After 更大时听上游。"""
        h = hook(mode="cooldown", cooldownMs=5000)
        self.assertEqual(h.decide(429, 1), {"action": "retry", "waitMs": 5000, "mode": "cooldown"})
        self.assertEqual(h.decide(429, 1, retry_after_ms=9000)["waitMs"], 9000)
        self.assertEqual(h.decide(429, 1, retry_after_ms=1000)["waitMs"], 5000, "上游给得短不缩短")

        deaf = hook(mode="cooldown", cooldownMs=5000, respectRetryAfter=False)
        self.assertEqual(deaf.decide(429, 1, retry_after_ms=9000)["waitMs"], 5000)

    def test_next_minute_mode(self) -> None:
        """按墙上时钟对齐，Retry-After 跨过边界时顺延。"""
        now = [30000.0]
        h = hook(wall_now=lambda: now[0], mode="nextMinute", nextMinuteOffsetMs=1000)
        self.assertEqual(h.decide(429, 1), {"action": "retry", "waitMs": 31000, "mode": "nextMinute"})

        now[0] = 59000.0
        self.assertEqual(h.decide(429, 1, retry_after_ms=5000)["waitMs"], 5000 + 57000)

    def test_passthrough_reasons(self) -> None:
        """次数用尽 / 未启用 / 非限流状态码：透传。"""
        h = hook(maxRetries=2)
        self.assertEqual(h.decide(429, 1)["action"], "retry")
        self.assertEqual(h.decide(429, 2)["action"], "retry")
        self.assertEqual(h.decide(429, 3), {"action": "passthrough", "reason": "max-retries"})
        self.assertEqual(h.stats["exhausted"], 1)

        self.assertEqual(hook(enabled=False).decide(429, 1)["reason"], "disabled")
        self.assertEqual(hook(maxRetries=0).decide(429, 1)["reason"], "retry-disabled")
        self.assertEqual(hook().decide(503, 1)["reason"], "not-retryable")
        self.assertEqual(hook(statuses=[503]).decide(503, 1)["action"], "retry")

    def test_budget_and_stash_cap(self) -> None:
        """暂存预算与暂存上限：超了就透传。"""
        h = hook(cooldownMs=10000, maxHoldMs=25000, maxRetries=5)
        self.assertEqual(h.decide(429, 1, held_ms=0)["action"], "retry")
        self.assertEqual(h.decide(429, 2, held_ms=10000)["action"], "retry")
        self.assertEqual(h.decide(429, 3, held_ms=20000)["reason"], "hold-budget")

        full = hook(maxStashed=1)
        at = full.enter(1000)
        self.assertEqual(full.decide(429, 1)["reason"], "stash-full")
        full.leave(at, 1000)
        self.assertEqual(full.decide(429, 1)["action"], "retry")

    def test_request_overrides(self) -> None:
        """x-gate-max-retries / x-gate-retry-mode。"""
        self.assertEqual(
            request_overrides(_Headers({"x-gate-max-retries": "5", "x-gate-retry-mode": "NEXTMINUTE"})),
            {"maxRetries": 5, "mode": "nextMinute"},
        )
        self.assertEqual(
            request_overrides(_Headers({"x-gate-max-retries": "-1", "x-gate-retry-mode": "bogus"})),
            {}, "非法值忽略",
        )
        self.assertEqual(request_overrides(_Headers()), {})
        self.assertEqual(request_overrides(None), {})

        h = hook(wall_now=lambda: 30000.0, maxRetries=1, mode="cooldown", cooldownMs=100)
        ov = {"maxRetries": 3, "mode": "nextMinute"}
        self.assertEqual(h.decide(429, 2, overrides=ov)["action"], "retry")
        self.assertEqual(h.decide(429, 1, overrides=ov)["mode"], "nextMinute")
        self.assertEqual(h.decide(429, 1, overrides={"maxRetries": 0})["reason"], "retry-disabled")

    def test_snapshot(self) -> None:
        """暂存数、下一次放行倒计时、统计。"""
        now = [1000.0]
        h = hook(wall_now=lambda: now[0])
        a = h.enter(5000)
        h.enter(8000)
        snap = h.snapshot()
        self.assertEqual(snap["stashed"], 2)
        self.assertEqual(snap["nextReleaseInMs"], 5000)
        self.assertEqual(snap["stats"]["peakStashed"], 2)

        now[0] = 3000.0
        h.leave(a, 2000)
        snap = h.snapshot()
        self.assertEqual(snap["stashed"], 1)
        self.assertEqual(snap["nextReleaseInMs"], 6000)
        self.assertEqual(snap["stats"]["heldMsTotal"], 2000)

    def test_thread_safe_counters(self) -> None:
        """多线程同时 enter/leave，计数不串。"""
        h = hook(maxStashed=1000)

        def churn() -> None:
            for _ in range(50):
                at = h.enter(10)
                h.leave(at, 1)

        threads = [threading.Thread(target=churn) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(h.stashed, 0)
        self.assertEqual(h.stats["stashed"], 400)


class TestRetryConfig(unittest.TestCase):
    def test_defaults_and_shorthand(self) -> None:
        cfg = load_config(file=None, env={})
        self.assertEqual(cfg["retry"], DEFAULT_RETRY)

        self.assertFalse(normalize_retry(False)["enabled"])
        self.assertEqual(normalize_retry({"cooldownSeconds": 30})["cooldownMs"], 30000)
        self.assertEqual(normalize_retry({"cooldown": "2m"})["cooldownMs"], 120000)
        self.assertEqual(normalize_retry({"statuses": "429, 503"})["statuses"], [429, 503])
        self.assertNotIn("typo", normalize_retry({"typo": 1}))
        # JSON 里写 2.0 在 Python 里是 float，要和 Node 行为对齐
        self.assertEqual(normalize_retry({"maxRetries": 2.0})["maxRetries"], 2)

    def test_env_overrides(self) -> None:
        cfg = load_config(file=None, env={
            "GATE_MAX_RETRIES": "4",
            "GATE_RETRY_MODE": "nextMinute",
            "GATE_RETRY_COOLDOWN_SECONDS": "20",
            "GATE_RETRY_ENABLED": "yes",
            "GATE_RETRY_NEXT_MINUTE_OFFSET_MS": "0",
        })
        self.assertEqual(cfg["retry"]["maxRetries"], 4)
        self.assertEqual(cfg["retry"]["mode"], "nextMinute")
        self.assertEqual(cfg["retry"]["cooldownMs"], 20000)
        self.assertTrue(cfg["retry"]["enabled"])
        self.assertEqual(cfg["retry"]["nextMinuteOffsetMs"], 0)

    def test_invalid(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"retry\.mode"):
            normalize_retry({"mode": "sometimes"})
        with self.assertRaisesRegex(ConfigError, r"retry\.maxRetries"):
            normalize_retry({"maxRetries": -1})
        with self.assertRaisesRegex(ConfigError, r"retry\.statuses"):
            normalize_retry({"statuses": []})
        with self.assertRaisesRegex(ConfigError, "状态码不合法"):
            normalize_retry({"statuses": [42]})
        with self.assertRaisesRegex(ConfigError, "小于 60000"):
            normalize_retry({"nextMinuteOffsetMs": 60000})
        with self.assertRaisesRegex(ConfigError, r"true/false"):
            load_config(file=None, env={"GATE_RETRY_ENABLED": "maybe"})

    def test_upstreams(self) -> None:
        self.assertEqual(
            normalize_upstreams({"a": "https://x.example.com/", "b": "http://127.0.0.1:9/v1"}),
            {"a": "https://x.example.com", "b": "http://127.0.0.1:9/v1"},
        )
        self.assertEqual(normalize_upstreams('{"a":"http://h"}'), {"a": "http://h"})
        with self.assertRaisesRegex(ConfigError, "名字"):
            normalize_upstreams({"bad name": "http://h"})
        with self.assertRaisesRegex(ConfigError, "http/https"):
            normalize_upstreams({"a": "ftp://h"})
        cfg = load_config(file=None, env={}, overrides={"upstreams": {"k": "http://k:1"}})
        self.assertEqual(cfg["upstreams"], {"k": "http://k:1"})


class TestSchedulerPriority(unittest.TestCase):
    def _scheduler(self, **overrides: Any) -> Scheduler:
        cfg = {
            "effectiveLimits": [{"windowMs": 60000, "max": 1000}],
            "maxConcurrent": 1,
            "minGapMs": 0,
            "maxQueue": 1,
            "queueTimeoutMs": 5000,
            "penaltyMs": 100,
            "maxPenaltyMs": 500,
        }
        cfg.update(overrides)
        return Scheduler(cfg, logger=NullLogger())

    def test_priority_jumps_queue(self) -> None:
        """priority 插到普通请求前面，且不受 maxQueue 限制。"""
        s = self._scheduler()
        order: List[str] = []
        lock = threading.Lock()
        started = threading.Event()

        def take(name: str, **kw: Any) -> None:
            try:
                slot = s.acquire({"name": name}, **kw)
            except GateError:
                return
            with lock:
                order.append(name)
            if name == "A":
                started.set()
                time.sleep(0.25)
            slot.release()

        a = threading.Thread(target=take, args=("A",), daemon=True)
        a.start()
        self.assertTrue(started.wait(timeout=3))

        threads = [
            threading.Thread(target=take, args=("B",), daemon=True),
            threading.Thread(target=take, args=("C",), kwargs={"priority": True}, daemon=True),
        ]
        threads[0].start()
        time.sleep(0.05)   # 确保 B 先排上
        threads[1].start()
        time.sleep(0.05)
        self.assertEqual(s.snapshot()["queuedPriority"], 1)

        a.join(timeout=5)
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(order, ["A", "C", "B"], "C 虽然后到，但插在 B 前面")
        s.close()

    def test_priority_ignores_max_queue(self) -> None:
        """maxQueue 占满时，priority 仍能排进来。"""
        s = self._scheduler(maxQueue=1)
        held = s.acquire({"name": "held"})
        blocked = threading.Thread(
            target=lambda: self._swallow(s, {"name": "fills-queue"}), daemon=True)
        blocked.start()
        time.sleep(0.08)
        self.assertEqual(s.snapshot()["queued"], 1, "队列已满")

        with self.assertRaises(GateError):
            s.acquire({"name": "rejected"}, poll_ms=10, cancel_check=lambda: False)

        done = threading.Event()

        def pri() -> None:
            slot = s.acquire({"name": "pri"}, priority=True)
            done.set()
            slot.release()

        t = threading.Thread(target=pri, daemon=True)
        t.start()
        time.sleep(0.05)
        self.assertEqual(s.snapshot()["queuedPriority"], 1, "priority 不被 maxQueue 拒掉")
        held.release()
        self.assertTrue(done.wait(timeout=3))
        s.close()
        blocked.join(timeout=3)
        t.join(timeout=3)

    @staticmethod
    def _swallow(s: Scheduler, meta: Dict[str, Any]) -> None:
        try:
            s.acquire(meta).release()
        except GateError:
            pass

    def test_exact_cooldown(self) -> None:
        """exactMs：不做指数退避、不被 maxPenaltyMs 削顶。"""
        s = self._scheduler(penaltyMs=100, maxPenaltyMs=500)
        self.assertEqual(s.penalize(None, exact_ms=2000), 2000)
        self.assertGreater(s.next_delay_ms(), 1800)
        self.assertEqual(s.penalize(None, exact_ms=0), 0)
        s.close()

    def test_priority_timeout_override(self) -> None:
        """priority 请求可以单独放宽排队超时。"""
        s = self._scheduler(queueTimeoutMs=80)
        held = s.acquire({"name": "held"})
        result: List[Optional[str]] = [None]

        def pri() -> None:
            try:
                slot = s.acquire({"name": "pri"}, priority=True, timeout_ms=4000)
                result[0] = "ok"
                slot.release()
            except GateError as err:
                result[0] = err.code

        t = threading.Thread(target=pri, daemon=True)
        t.start()
        time.sleep(0.3)
        self.assertIsNone(result[0], "超过默认 80ms 也没被踢掉")
        held.release()
        t.join(timeout=3)
        self.assertEqual(result[0], "ok")
        s.close()


if __name__ == "__main__":
    unittest.main()
