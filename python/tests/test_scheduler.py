"""调度器的单元测试（与 Node 版 test/scheduler.test.js 对应）。

Python 版是多线程阻塞模型，没法像 Node 那样注入假时钟后同步推进时间，
所以这里用「可手动推进的单调时钟 + 后台线程」来测：
时钟由测试线程推进，调度器里排队的线程醒来后看到的是推进后的时间。
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Dict, List, Optional

from gate.logger import NullLogger
from gate.scheduler import GateError, Scheduler


def make_cfg(**overrides: Any) -> Dict[str, Any]:
    """手工拼一份配置，不走 load_config（避免被真实 gate.config.json 影响）。"""
    cfg: Dict[str, Any] = {
        "rpm": 1000,
        "windowMs": 60000,
        "limits": [],
        "maxConcurrent": 2,
        "minGapMs": 0,
        "maxQueue": 200,
        "queueTimeoutMs": 5000,
        "penaltyMs": 100,
        "maxPenaltyMs": 400,
    }
    cfg.update(overrides)
    if "effectiveLimits" not in cfg:
        cfg["effectiveLimits"] = [{"windowMs": cfg["windowMs"], "max": cfg["rpm"]}, *cfg["limits"]]
    return cfg


class StepClock:
    """手动推进的毫秒时钟。推进后会叫醒调度器里等着的线程。"""

    def __init__(self, scheduler_ref: Optional[List[Scheduler]] = None) -> None:
        self._ms = 1000.0
        self._lock = threading.Lock()
        self._ref = scheduler_ref if scheduler_ref is not None else []

    def __call__(self) -> float:
        with self._lock:
            return self._ms

    def advance(self, ms: float) -> None:
        with self._lock:
            self._ms += ms
        # 时间变了，叫醒所有在 cond 上等的线程重新判断
        for sched in self._ref:
            with sched._cond:  # noqa: SLF001 测试内部探针
                sched._cond.notify_all()


class Runner:
    """在后台线程里调 acquire，记录结果，方便断言。"""

    def __init__(self, scheduler: Scheduler, label: str, **kwargs: Any) -> None:
        self.scheduler = scheduler
        self.label = label
        self.kwargs = kwargs
        self.slot: Any = None
        self.error: Optional[BaseException] = None
        self.admitted_at: Optional[float] = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.slot = self.scheduler.acquire({"window": self.label}, **self.kwargs)
            self.admitted_at = self.scheduler._now()  # noqa: SLF001 测试内部探针
        except BaseException as err:  # noqa: BLE001 要把异常带回主线程断言
            self.error = err
        finally:
            self.done.set()

    def wait(self, timeout: float = 2.0) -> bool:
        return self.done.wait(timeout)

    def release(self) -> None:
        if self.slot is not None:
            self.slot.release()


def settle(seconds: float = 0.05) -> None:
    """给后台线程一点时间把状态推进到稳定。"""
    time.sleep(seconds)


def wait_until(predicate: Any, timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class SchedulerTestBase(unittest.TestCase):
    def build(self, **overrides: Any) -> tuple[Scheduler, StepClock]:
        ref: List[Scheduler] = []
        clock = StepClock(ref)
        sched = Scheduler(make_cfg(**overrides), logger=NullLogger(), now_ms=clock)
        ref.append(sched)
        self.addCleanup(sched.close)
        return sched, clock


class TestConcurrency(SchedulerTestBase):
    def test_max_concurrent(self) -> None:
        """并发上限：活跃请求数永不超过 maxConcurrent。"""
        sched, _clock = self.build(maxConcurrent=2)
        runners = [Runner(sched, f"w{i}") for i in range(5)]

        self.assertTrue(wait_until(lambda: sched.in_flight == 2))
        settle()
        self.assertEqual(sched.in_flight, 2, "并发数不能超过 2")
        self.assertEqual(sum(1 for r in runners if r.done.is_set()), 2)

        # 一个个放掉名额，每放掉一个才多放行一个，全程并发不超过 2
        released = 0
        while released < 5:
            holder = next((r for r in runners if r.done.is_set() and r.slot is not None), None)
            self.assertIsNotNone(holder, "应该总有人持有名额")
            self.assertLessEqual(sched.in_flight, 2, "并发数不能超过 2")
            holder.slot.release()
            holder.slot = None
            released += 1
            if released < 5:
                self.assertTrue(
                    wait_until(lambda n=released: sched.stats["admitted"] >= min(5, n + 2)),
                    "释放名额后应放行下一个",
                )

        for r in runners:
            self.assertTrue(r.wait(), f"{r.label} 最终应被放行")
        self.assertEqual(sched.stats["admitted"], 5)
        self.assertEqual(sched.in_flight, 0)

    def test_release_idempotent(self) -> None:
        """release 幂等：重复调用不会把并发数扣穿。"""
        sched, _clock = self.build(maxConcurrent=1)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        a.slot.release()
        a.slot.release()
        a.slot.release()
        self.assertEqual(sched.in_flight, 0)

    def test_slot_context_manager(self) -> None:
        """with slot: 退出时自动 release。"""
        sched, _clock = self.build(maxConcurrent=1)
        slot = sched.acquire({"window": "a"})
        with slot:
            self.assertEqual(sched.in_flight, 1)
        self.assertEqual(sched.in_flight, 0)


class TestWindows(SchedulerTestBase):
    def test_single_window(self) -> None:
        """每 X 毫秒 Y 次：窗口打满后要等最早一次滚出去。"""
        sched, clock = self.build(
            maxConcurrent=10, rpm=3, windowMs=300, effectiveLimits=[{"windowMs": 300, "max": 3}]
        )
        runners = [Runner(sched, f"w{i}") for i in range(4)]

        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 3))
        settle()
        self.assertEqual(sched.stats["admitted"], 3, "窗口内只放 3 个")
        pending = [r for r in runners if not r.done.is_set()]
        self.assertEqual(len(pending), 1)

        clock.advance(299)
        settle()
        self.assertEqual(sched.stats["admitted"], 3, "窗口还没滚出去，第 4 个继续等")

        clock.advance(2)
        self.assertTrue(pending[0].wait(), "窗口滚出后第 4 个应被放行")
        self.assertEqual(sched.stats["admitted"], 4)
        for r in runners:
            r.release()

    def test_multiple_windows(self) -> None:
        """多条限制同时生效：每 1000 毫秒 10 次 + 每 100 毫秒 2 次。"""
        sched, clock = self.build(
            maxConcurrent=10,
            rpm=10,
            windowMs=1000,
            effectiveLimits=[{"windowMs": 100, "max": 2}, {"windowMs": 1000, "max": 10}],
        )
        runners = [Runner(sched, f"w{i}") for i in range(6)]

        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 2))
        settle()
        self.assertEqual(sched.stats["admitted"], 2, "100ms 窗口只放 2 个")

        clock.advance(101)
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 4))
        settle()
        self.assertEqual(sched.stats["admitted"], 4, "下一个 100ms 窗口再放 2 个")

        clock.advance(101)
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 6))
        for r in runners:
            self.assertTrue(r.wait())
            r.release()

        # 1 秒窗口的 10 次名额还剩 4 个
        snap = sched.snapshot()
        long_window = [l for l in snap["limits"] if l["windowMs"] == 1000][0]
        self.assertEqual(long_window["used"], 6)

    def test_long_window_caps_total(self) -> None:
        """长窗口兜底：短窗口还有名额，长窗口打满也得等。"""
        sched, clock = self.build(
            maxConcurrent=10,
            rpm=3,
            windowMs=1000,
            effectiveLimits=[{"windowMs": 100, "max": 2}, {"windowMs": 1000, "max": 3}],
        )
        runners = [Runner(sched, f"w{i}") for i in range(4)]
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 2))

        clock.advance(101)
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 3))
        settle()
        self.assertEqual(sched.stats["admitted"], 3, "1 秒窗口只有 3 个名额")

        clock.advance(500)
        settle()
        self.assertEqual(sched.stats["admitted"], 3, "1 秒窗口还没滚完")

        clock.advance(400)
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 4))
        for r in runners:
            self.assertTrue(r.wait())
            r.release()

    def test_min_gap(self) -> None:
        """最小间隔：相邻两次放行至少隔 minGapMs。"""
        sched, clock = self.build(maxConcurrent=10, minGapMs=50)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        b = Runner(sched, "b")
        settle()
        self.assertFalse(b.done.is_set(), "间隔没到，第二个要等")

        clock.advance(49)
        settle()
        self.assertFalse(b.done.is_set())

        clock.advance(2)
        self.assertTrue(b.wait())
        self.assertGreaterEqual(b.admitted_at - a.admitted_at, 50)
        a.release()
        b.release()


class TestPenalty(SchedulerTestBase):
    def test_cooldown(self) -> None:
        """429 冷却：penalize 之后所有窗口一起等。"""
        sched, clock = self.build(maxConcurrent=10, penaltyMs=100, maxPenaltyMs=400)
        first = Runner(sched, "a")
        self.assertTrue(first.wait())
        first.release()

        waited = sched.penalize(200)
        self.assertEqual(waited, 200, "Retry-After 比基数大时听上游的")

        b = Runner(sched, "b")
        settle()
        self.assertFalse(b.done.is_set(), "冷却中不放行")

        clock.advance(180)
        settle()
        self.assertFalse(b.done.is_set())

        clock.advance(25)
        self.assertTrue(b.wait(), "冷却结束后放行")
        b.release()
        self.assertEqual(sched.stats["penalties"], 1)

    def test_backoff_exponential(self) -> None:
        """连续 429：指数退避，但不超过 maxPenaltyMs。"""
        sched, _clock = self.build(penaltyMs=100, maxPenaltyMs=400)
        self.assertEqual(sched.penalize(), 100)
        self.assertEqual(sched.penalize(), 200)
        self.assertEqual(sched.penalize(), 400)
        self.assertEqual(sched.penalize(), 400, "封顶在 maxPenaltyMs")
        self.assertEqual(sched.penalize(99999), 400, "Retry-After 也不能超过上限")
        self.assertEqual(sched.penalty_streak, 5)

    def test_on_success_resets_streak(self) -> None:
        sched, _clock = self.build(penaltyMs=100, maxPenaltyMs=400)
        sched.penalize()
        sched.penalize()
        sched.on_success()
        self.assertEqual(sched.penalty_streak, 0)
        self.assertEqual(sched.penalize(), 100, "重置后退避从基数重新开始")


class TestFairness(SchedulerTestBase):
    def test_fifo(self) -> None:
        """公平调度：FIFO，排在后面的窗口不会被饿死。"""
        sched, clock = self.build(maxConcurrent=1, minGapMs=10)
        order: List[str] = []
        lock = threading.Lock()

        def worker(label: str) -> None:
            slot = sched.acquire({"window": label})
            with lock:
                order.append(label)
            slot.release()

        # A 先排 3 个，B 最后排 1 个
        threads = []
        for label in ("A1", "A2", "A3", "B1"):
            t = threading.Thread(target=worker, args=(label,), daemon=True)
            threads.append(t)
            t.start()
            # 保证入队顺序确定
            self.assertTrue(wait_until(lambda n=len(threads): sched.snapshot()["queued"] + sched.stats["admitted"] >= n))

        for _ in range(8):
            clock.advance(10)
            settle(0.02)

        for t in threads:
            t.join(timeout=2)
        self.assertEqual(order, ["A1", "A2", "A3", "B1"], "严格先来先得")


class TestQueueLimits(SchedulerTestBase):
    def test_queue_full(self) -> None:
        """队列上限：超过 maxQueue 直接 503，不无限堆积。"""
        sched, _clock = self.build(maxConcurrent=1, maxQueue=2)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        b = Runner(sched, "b")
        c = Runner(sched, "c")
        self.assertTrue(wait_until(lambda: sched.snapshot()["queued"] == 2))

        with self.assertRaises(GateError) as ctx:
            sched.acquire({"window": "d"})
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "QUEUE_FULL")
        self.assertEqual(sched.stats["rejected"], 1)

        a.release()
        self.assertTrue(b.wait())
        b.release()
        self.assertTrue(c.wait())
        c.release()

    def test_queue_timeout(self) -> None:
        """排队超时：超过 queueTimeoutMs 返回 504。"""
        sched, clock = self.build(maxConcurrent=1, queueTimeoutMs=200)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        b = Runner(sched, "b")
        settle()
        self.assertFalse(b.done.is_set())

        clock.advance(201)
        self.assertTrue(b.wait(), "超时后应该返回")
        self.assertIsInstance(b.error, GateError)
        self.assertEqual(b.error.status, 504)
        self.assertEqual(b.error.code, "QUEUE_TIMEOUT")
        self.assertEqual(sched.stats["timedOut"], 1)
        a.release()

    def test_cancel_frees_slot(self) -> None:
        """客户端断开：排队中取消立即出队，不占名额。"""
        sched, _clock = self.build(maxConcurrent=1)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())

        gone = threading.Event()
        b = Runner(sched, "b", cancel_check=gone.is_set, poll_ms=10)
        c = Runner(sched, "c")
        self.assertTrue(wait_until(lambda: sched.snapshot()["queued"] == 2))

        gone.set()
        self.assertTrue(b.wait(), "取消后立刻返回")
        self.assertIsInstance(b.error, GateError)
        self.assertEqual(b.error.status, 499)
        self.assertEqual(b.error.code, "CLIENT_ABORTED")
        self.assertEqual(sched.stats["aborted"], 1)

        a.release()
        self.assertTrue(c.wait(), "名额让给了排在后面的 c")
        c.release()
        self.assertEqual(sched.stats["admitted"], 2, "被取消的那个没占名额")

    def test_cancel_by_ticket(self) -> None:
        """按票号取消还在排队的请求。"""
        sched, _clock = self.build(maxConcurrent=1)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        b = Runner(sched, "b", poll_ms=10)
        self.assertTrue(wait_until(lambda: sched.snapshot()["queued"] == 1))

        # 队首已经被 a 占走，b 的票号是第 2 张
        self.assertTrue(sched.cancel(2))
        self.assertTrue(b.wait())
        self.assertIsInstance(b.error, GateError)
        self.assertEqual(b.error.code, "CLIENT_ABORTED")
        self.assertFalse(sched.cancel(999), "不存在的票号返回 False")
        a.release()

    def test_close_rejects_everything(self) -> None:
        """关闭闸门：排队中的请求全部收到 503。"""
        sched, _clock = self.build(maxConcurrent=1)
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        b = Runner(sched, "b")
        c = Runner(sched, "c")
        self.assertTrue(wait_until(lambda: sched.snapshot()["queued"] == 2))

        sched.close()
        self.assertTrue(b.wait())
        self.assertTrue(c.wait())
        for r in (b, c):
            self.assertIsInstance(r.error, GateError)
            self.assertEqual(r.error.status, 503)
            self.assertEqual(r.error.code, "GATE_CLOSED")

        with self.assertRaises(GateError) as ctx:
            sched.acquire({"window": "d"})
        self.assertEqual(ctx.exception.code, "GATE_CLOSED")
        a.release()


class TestCompatAndSnapshot(SchedulerTestBase):
    def test_legacy_rpm_only(self) -> None:
        """兼容旧配置：只给 rpm/windowMs（没有 effectiveLimits）也能正常限流。"""
        cfg = make_cfg(maxConcurrent=10, rpm=2, windowMs=200)
        cfg.pop("effectiveLimits")
        ref: List[Scheduler] = []
        clock = StepClock(ref)
        sched = Scheduler(cfg, logger=NullLogger(), now_ms=clock)
        ref.append(sched)
        self.addCleanup(sched.close)

        runners = [Runner(sched, f"w{i}") for i in range(3)]
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 2))
        settle()
        self.assertEqual(sched.stats["admitted"], 2)

        clock.advance(201)
        self.assertTrue(wait_until(lambda: sched.stats["admitted"] == 3))
        for r in runners:
            self.assertTrue(r.wait())
            r.release()

    def test_snapshot(self) -> None:
        """快照：报告窗口用量与下一个名额的等待时间。"""
        sched, clock = self.build(
            maxConcurrent=10,
            rpm=2,
            windowMs=1000,
            minGapMs=5,
            effectiveLimits=[{"windowMs": 1000, "max": 2}],
        )
        a = Runner(sched, "a")
        self.assertTrue(a.wait())
        clock.advance(10)
        b = Runner(sched, "b")
        self.assertTrue(b.wait())

        snap = sched.snapshot()
        self.assertEqual(snap["inFlight"], 2)
        self.assertEqual(snap["queued"], 0)
        self.assertEqual(snap["maxConcurrent"], 10)
        self.assertEqual(snap["minGapMs"], 5)
        self.assertEqual(len(snap["limits"]), 1)
        self.assertEqual(snap["limits"][0]["used"], 2)
        self.assertEqual(snap["limits"][0]["max"], 2)
        # 窗口打满，第一次放行还要 990ms 才滚出去
        self.assertAlmostEqual(snap["limits"][0]["resetInMs"], 990, delta=2)
        self.assertEqual(snap["penaltyMsLeft"], 0)
        self.assertEqual(snap["stats"]["admitted"], 2)

        sched.penalize(300)
        snap2 = sched.snapshot()
        self.assertAlmostEqual(snap2["penaltyMsLeft"], 300, delta=2)
        self.assertEqual(snap2["penaltyStreak"], 1)
        a.release()
        b.release()

    def test_next_delay_ms(self) -> None:
        """next_delay_ms：0 能放、inf 表示并发打满。"""
        sched, clock = self.build(maxConcurrent=1, minGapMs=50)
        self.assertEqual(sched.next_delay_ms(), 0)
        slot = sched.acquire({"window": "a"})
        self.assertEqual(sched.next_delay_ms(), float("inf"), "并发打满")
        slot.release()
        self.assertAlmostEqual(sched.next_delay_ms(), 50, delta=2)
        clock.advance(51)
        self.assertEqual(sched.next_delay_ms(), 0)


if __name__ == "__main__":
    unittest.main()
