"""代理层的 429 暂存/重试端到端测试（与 Node 版 test/retry-proxy.test.js 对应）。

复用 test_proxy.py 里的假上游与请求工具，只是闸门这次挂上重试钩子。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

from gate.config import DEFAULT_RETRY
from gate.logger import NullLogger
from gate.proxy import resolve_upstream
from gate.retry import RetryHook
from gate.server import build

# `unittest discover -s tests` 会把 tests/ 当顶层目录（模块名没有包前缀），
# `python -m unittest tests.test_retry_proxy` 又是按包导入，两种都要能跑
try:
    from .test_proxy import FakeUpstream, http_request, make_cfg, parallel, wait_until
except ImportError:  # pragma: no cover - 取决于启动方式
    from test_proxy import FakeUpstream, http_request, make_cfg, parallel, wait_until


class RetryGate:
    """带重试钩子的闸门。retry 里的字段覆盖 DEFAULT_RETRY。"""

    def __init__(
        self,
        test: unittest.TestCase,
        upstream: str,
        retry: Optional[Dict[str, Any]] = None,
        wall_now: Any = None,
        **overrides: Any,
    ) -> None:
        cfg = make_cfg(upstream, **overrides)
        cfg["retry"] = {**DEFAULT_RETRY, "cooldownMs": 60, "maxHoldMs": 5000, **(retry or {})}
        # build() 自己会建钩子，但测试要注入假墙上时钟，所以先建好再换掉
        self.server, self.scheduler, _log, self.cfg = build(cfg, NullLogger(), with_retry=False)
        self.retry_hook = RetryHook(cfg["retry"], logger=NullLogger(), wall_now_ms=wall_now)
        # 重新生成 handler，把钩子挂上
        from gate.proxy import make_handler_class
        self.server.RequestHandlerClass = make_handler_class(
            self.cfg, self.scheduler, NullLogger(), self.retry_hook)
        self.port: int = self.cfg["port"]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.scheduler.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LimitedUpstream:
    """前 n 次回 429（或指定状态码），之后回 200。"""

    def __init__(
        self,
        test: unittest.TestCase,
        n: int,
        retry_after: Optional[str] = None,
        status: int = 429,
    ) -> None:
        self.up = FakeUpstream(test)
        self.hits = 0
        self.bodies: List[str] = []
        self._lock = threading.Lock()
        self._n = n
        self._retry_after = retry_after
        self._status = status
        self.up.responder = self._respond

    def _respond(self, handler: Any, body: bytes) -> None:
        with self._lock:
            self.hits += 1
            n = self.hits
            self.bodies.append(body.decode("utf-8", "replace"))
        if n <= self._n:
            payload = b'{"type":"error","error":{"type":"rate_limit_error"}}'
            handler.send_response(self._status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("X-Attempt", str(n))
            if self._retry_after is not None:
                handler.send_header("Retry-After", self._retry_after)
            handler.end_headers()
            handler.wfile.write(payload)
            return
        payload = json.dumps({"ok": True, "attempt": n}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @property
    def url(self) -> str:
        return self.up.url


class TestStashRetry(unittest.TestCase):
    def test_429_retried_client_sees_200(self) -> None:
        """429 暂存重试：客户端只看到最终的 200，上游被打了 2 次。"""
        up = LimitedUpstream(self, 1)
        gate = RetryGate(self, up.url, retry={"mode": "cooldown", "cooldownMs": 60})

        status, _headers, data = http_request(gate.port, body=b'{"model":"claude","n":1}')
        self.assertEqual(status, 200, "429 被闸门自己扛掉，客户端看不到")
        self.assertEqual(json.loads(data)["attempt"], 2)
        self.assertEqual(up.hits, 2)
        self.assertEqual(up.bodies, ['{"model":"claude","n":1}'] * 2, "重试时请求体原样重放")

        # 计数是在响应写完之后更新的，客户端可能先拿到包，这里等一下
        self.assertTrue(
            wait_until(lambda: gate.retry_hook.snapshot()["stats"]["recovered"] == 1, timeout=3),
            "重试成功应计入 recovered",
        )
        snap = gate.retry_hook.snapshot()
        self.assertEqual(snap["stats"]["retried"], 1)
        self.assertEqual(snap["stats"]["stashed"], 1)
        self.assertEqual(snap["stashed"], 0, "放行后暂存计数归零")
        self.assertGreaterEqual(gate.scheduler.stats["penalties"], 1, "暂存期间全局冷却也生效")

    def test_exhausted_passes_through_untouched(self) -> None:
        """重试次数用尽：把上游最后那个 429 原样透传。"""
        up = LimitedUpstream(self, 99, retry_after="1")
        gate = RetryGate(self, up.url, retry={"maxRetries": 2, "cooldownMs": 40})

        status, headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("retry-after"), "1", "上游的 Retry-After 带给客户端")
        self.assertEqual(headers.get("x-attempt"), "3", "回的是最后一次的响应头")
        self.assertIn(b"rate_limit_error", data, "上游错误体不被改写")
        self.assertEqual(up.hits, 3, "首次 + 2 次重试")

        snap = gate.retry_hook.snapshot()
        self.assertEqual(snap["stats"]["retried"], 2)
        self.assertEqual(snap["stats"]["exhausted"], 1)
        self.assertEqual(snap["stats"]["recovered"], 0)

    def test_529_retried_503_untouched(self) -> None:
        """529 也走暂存重试；503 不碰。"""
        up529 = LimitedUpstream(self, 1, status=529)
        gate529 = RetryGate(self, up529.url, retry={"cooldownMs": 40})
        self.assertEqual(http_request(gate529.port, body=b"{}")[0], 200)
        self.assertEqual(up529.hits, 2)

        up503 = LimitedUpstream(self, 1, status=503)
        gate503 = RetryGate(self, up503.url)
        status, _h, _d = http_request(gate503.port, body=b"{}")
        self.assertEqual(status, 503, "503 不算限流，原样透传")
        self.assertEqual(up503.hits, 1, "不重试")
        self.assertEqual(gate503.scheduler.stats["penalties"], 0)
        self.assertEqual(gate503.retry_hook.snapshot()["stats"]["retried"], 0)

    def test_next_minute_mode(self) -> None:
        """nextMinute 模式：等到下一个 :01 再重试，不等满一分钟。"""
        up = LimitedUpstream(self, 1)
        # 假墙上时钟停在 :59.90，offset 调小让测试跑得快 → 等约 200ms
        gate = RetryGate(
            self, up.url,
            wall_now=lambda: 59900.0,
            retry={"mode": "nextMinute", "nextMinuteOffsetMs": 100},
        )
        started = time.monotonic()
        status, _h, _d = http_request(gate.port, body=b"{}")
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertEqual(up.hits, 2)
        self.assertGreaterEqual(elapsed, 0.18, f"应等约 200ms 才重试，实际 {elapsed:.3f}s")
        self.assertLess(elapsed, 3.0, f"不该等满一分钟，实际 {elapsed:.3f}s")

    def test_header_override(self) -> None:
        """x-gate-max-retries=0 时直接透传 429。"""
        up = LimitedUpstream(self, 99)
        gate = RetryGate(self, up.url, retry={"maxRetries": 3, "cooldownMs": 40})

        status, _h, _d = http_request(gate.port, body=b"{}", headers={"x-gate-max-retries": "0"})
        self.assertEqual(status, 429)
        self.assertEqual(up.hits, 1, "这个请求不重试")

        status2, _h2, _d2 = http_request(gate.port, body=b"{}", headers={"x-gate-max-retries": "1"})
        self.assertEqual(status2, 429)
        self.assertEqual(up.hits, 3, "第二个请求按头里的 1 次重试")

    def test_client_abort_while_stashed(self) -> None:
        """暂存期间客户端断开：立刻放手，不再重试。"""
        up = LimitedUpstream(self, 99)
        gate = RetryGate(self, up.url, retry={"maxRetries": 5, "cooldownMs": 3000})

        doomed = http.client.HTTPConnection("127.0.0.1", gate.port, timeout=5)
        doomed.request("POST", "/v1/messages", body=b"{}")
        self.assertTrue(wait_until(lambda: gate.retry_hook.stashed == 1, timeout=5),
                        "应已进入暂存")
        self.assertEqual(up.hits, 1)

        doomed.close()
        self.assertTrue(wait_until(lambda: gate.retry_hook.stashed == 0, timeout=5),
                        "断开后立刻从暂存里出来")
        self.assertTrue(wait_until(lambda: gate.scheduler.snapshot()["queued"] == 0, timeout=3))
        time.sleep(0.15)
        self.assertEqual(up.hits, 1, "不会再往上游打")

    def test_stash_cap(self) -> None:
        """暂存上限：超过 maxStashed 的请求直接透传 429。"""
        up = LimitedUpstream(self, 99)
        gate = RetryGate(self, up.url, maxConcurrent=4,
                         retry={"maxRetries": 3, "cooldownMs": 2000, "maxStashed": 1})

        results = parallel([lambda: http_request(gate.port, body=b"{}") for _ in range(3)])
        statuses = [r[0] for r in results if isinstance(r, tuple)]
        self.assertGreaterEqual(statuses.count(429), 2,
                                f"暂存位只有 1 个，其余应透传，实际 {statuses}")
        self.assertGreaterEqual(gate.retry_hook.snapshot()["stats"]["passedThrough"], 2)

    def test_hold_budget(self) -> None:
        """暂存预算：maxHoldMs 用完后不再重试。"""
        up = LimitedUpstream(self, 99)
        gate = RetryGate(self, up.url, retry={"maxRetries": 10, "cooldownMs": 100, "maxHoldMs": 250})

        status, _h, _d = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 429)
        # 100 + 100 = 200 还在预算内，再加 100 就超了 → 最多重试 2 次
        self.assertEqual(up.hits, 3, f"应在预算用完时停手，实际打了 {up.hits} 次")
        self.assertEqual(gate.retry_hook.snapshot()["stats"]["passedThrough"], 1)

    def test_retry_after_beyond_budget(self) -> None:
        """上游 Retry-After 超出暂存预算：不硬等，直接透传。"""
        up = LimitedUpstream(self, 99, retry_after="7")
        gate = RetryGate(self, up.url, retry={"maxRetries": 3, "cooldownMs": 40, "maxHoldMs": 2000})

        status, headers, _d = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("retry-after"), "7")
        self.assertEqual(up.hits, 1, "一次都不重试")
        self.assertEqual(gate.retry_hook.snapshot()["stats"]["retried"], 0)

    def test_bypass_not_retried(self) -> None:
        """不排队的旁路请求不受重试影响。"""
        up = LimitedUpstream(self, 99)
        gate = RetryGate(self, up.url, retry={"maxRetries": 3, "cooldownMs": 40})

        status, _h, _d = http_request(
            gate.port, path="/v1/messages/count_tokens", body=b"{}")
        self.assertEqual(status, 429, "旁路请求原样透传，不做暂存")
        self.assertEqual(up.hits, 1)
        self.assertEqual(gate.retry_hook.snapshot()["stats"]["retried"], 0)


class TestRetryStatusEndpoint(unittest.TestCase):
    def test_status_includes_retry(self) -> None:
        up = LimitedUpstream(self, 0)
        gate = RetryGate(self, up.url, retry={"mode": "nextMinute", "maxRetries": 4})

        status, _headers, data = http_request(gate.port, method="GET", path="/__gate/status")
        self.assertEqual(status, 200)
        snap = json.loads(data)
        self.assertTrue(snap["retry"]["enabled"])
        self.assertEqual(snap["retry"]["mode"], "nextMinute")
        self.assertEqual(snap["retry"]["maxRetries"], 4)
        self.assertEqual(snap["retry"]["statuses"], [429, 529])
        self.assertEqual(snap["retry"]["stashed"], 0)
        self.assertEqual(snap["retry"]["nextReleaseInMs"], 0)
        self.assertEqual(snap["queuedPriority"], 0)
        self.assertEqual(up.hits, 0, "状态接口不转发上游")


class TestNamedUpstreams(unittest.TestCase):
    def test_resolve_upstream(self) -> None:
        ups = {"alt": "http://alt:1"}
        self.assertEqual(
            resolve_upstream("/v1/messages", "?a=1", ups, "http://dflt:2"),
            {"target": "http://dflt:2", "path": "/v1/messages?a=1", "name": ""},
        )
        self.assertEqual(
            resolve_upstream("/__gate/u/alt/v1/messages", "?a=1", ups, "http://dflt:2"),
            {"target": "http://alt:1", "path": "/v1/messages?a=1", "name": "alt"},
        )
        self.assertEqual(
            resolve_upstream("/__gate/u/alt", "", ups, "http://dflt:2"),
            {"target": "http://alt:1", "path": "/", "name": "alt"},
        )
        self.assertEqual(
            resolve_upstream("/__gate/u/nope/v1", "", ups, "http://dflt:2")["target"], None)

    def test_named_upstream_shares_queue(self) -> None:
        """/__gate/u/<名字>/... 转到对应上游，共用同一套限流。"""
        other = FakeUpstream(self)
        default = LimitedUpstream(self, 0)
        gate = RetryGate(self, default.url, upstreams={"alt": other.url})

        status, _h, data = http_request(gate.port, path="/__gate/u/alt/v1/messages?x=1", body=b"{}")
        self.assertEqual(status, 200)
        echo = json.loads(data)
        self.assertEqual(echo["path"], "/v1/messages?x=1", "前缀被剥掉，查询串保留")
        self.assertEqual(default.hits, 0, "没打到默认上游")
        self.assertEqual(gate.scheduler.stats["admitted"], 1, "命名上游也走同一个队列")

        missing, _h2, body2 = http_request(gate.port, path="/__gate/u/nope/v1/messages", body=b"{}")
        self.assertEqual(missing, 404)
        self.assertIn(b"nope", body2)

        dflt, _h3, _d3 = http_request(gate.port, body=b"{}")
        self.assertEqual(dflt, 200)
        self.assertEqual(default.hits, 1, "不带前缀仍走默认上游")


if __name__ == "__main__":
    unittest.main()
