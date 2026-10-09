"""代理层的端到端测试（与 Node 版 test/proxy.test.js 对应）。

起一个假上游 + 一个真闸门，用 http.client 打真实 HTTP 请求，
验证透传、SSE 不缓冲、排队、旁路、429 冷却、错误码等行为。
"""

from __future__ import annotations

import http.client
import json
import socket
import socketserver
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from gate.logger import NullLogger
from gate.proxy import build_client_headers, build_upstream_headers, parse_retry_after
from gate.server import build


# ---------- 假上游 ----------

class UpstreamState:
    """记录上游收到了什么，用于断言排队效果。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.hits: List[float] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self.lock = threading.Lock()


def json_responder(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    """默认响应：把收到的请求原样回显成 JSON。"""
    payload = json.dumps({
        "method": handler.command,
        "path": handler.path,
        "body": body.decode("utf-8", "replace"),
        "auth": handler.headers.get("Authorization"),
        "beta": handler.headers.get("anthropic-beta"),
        "host": handler.headers.get("Host"),
        "contentLength": handler.headers.get("Content-Length"),
        "transferEncoding": handler.headers.get("Transfer-Encoding"),
    }).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("X-Upstream-Marker", "1")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(payload)


def make_upstream_handler(state: UpstreamState, responder: Callable[[Any, bytes], None]) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            return  # 测试里不要刷 stderr

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with state.lock:
                state.in_flight += 1
                state.peak_in_flight = max(state.peak_in_flight, state.in_flight)
                state.hits.append(time.monotonic())
                state.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "headers": dict(self.headers.items()),
                })
            try:
                responder(self, body)
            finally:
                with state.lock:
                    state.in_flight -= 1

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_HEAD = _dispatch
        do_DELETE = _dispatch

    return Handler


class FastThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # 跳过 HTTPServer.server_bind 里的 getfqdn，Windows 上它可能卡好几秒
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class FakeUpstream:
    def __init__(self, test: unittest.TestCase, responder: Optional[Callable[[Any, bytes], None]] = None) -> None:
        self.state = UpstreamState()
        self.responder: Callable[[Any, bytes], None] = responder or json_responder
        handler = make_upstream_handler(self.state, lambda h, b: self.responder(h, b))
        self.server = FastThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port: int = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# ---------- 闸门 ----------

def make_cfg(upstream: str, **overrides: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "port": 0,  # 让系统分配，避免测试之间抢端口
        "host": "127.0.0.1",
        "upstream": upstream,
        "rpm": 1000,
        "windowMs": 60000,
        "limits": [],
        "maxConcurrent": 4,
        "minGapMs": 0,
        "maxQueue": 100,
        "queueTimeoutMs": 5000,
        "penaltyMs": 100,
        "maxPenaltyMs": 400,
        "bypassPaths": ["/v1/messages/count_tokens"],
        "logFile": "",
        "logLevel": "silent",
    }
    cfg.update(overrides)
    if "effectiveLimits" not in cfg:
        cfg["effectiveLimits"] = [{"windowMs": cfg["windowMs"], "max": cfg["rpm"]}, *cfg["limits"]]
    return cfg


class GateFixture:
    def __init__(self, test: unittest.TestCase, upstream: str, **overrides: Any) -> None:
        self.server, self.scheduler, _log, self.cfg = build(
            make_cfg(upstream, **overrides), NullLogger()
        )
        self.port: int = self.cfg["port"]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.scheduler.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


Response = Tuple[int, Dict[str, str], bytes]


def http_request(
    port: int,
    method: str = "POST",
    path: str = "/v1/messages",
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Response:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def parallel(fns: List[Callable[[], Any]]) -> List[Any]:
    """并发跑一批请求，按原顺序返回结果（异常也作为结果返回）。"""
    results: List[Any] = [None] * len(fns)

    def run(i: int, fn: Callable[[], Any]) -> None:
        try:
            results[i] = fn()
        except BaseException as err:  # noqa: BLE001 异常带回主线程断言
            results[i] = err

    threads = [threading.Thread(target=run, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------- 测试 ----------

class TestForwarding(unittest.TestCase):
    def test_passthrough(self) -> None:
        """透传：method / path / 查询串 / 头部 / 请求体 / 状态码。"""
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url)

        status, headers, data = http_request(
            gate.port,
            path="/v1/messages?beta=true",
            body=b'{"model":"claude"}',
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer token-123",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-upstream-marker"), "1")

        echo = json.loads(data)
        self.assertEqual(echo["method"], "POST")
        self.assertEqual(echo["path"], "/v1/messages?beta=true")
        self.assertEqual(echo["body"], '{"model":"claude"}')
        self.assertEqual(echo["auth"], "Bearer token-123", "鉴权头必须原样带过去")
        self.assertEqual(echo["beta"], "oauth-2025-04-20")
        self.assertEqual(echo["host"], f"127.0.0.1:{up.port}", "Host 换成上游的")
        self.assertEqual(echo["contentLength"], "18", "Content-Length 按实际请求体重算")
        self.assertIsNone(echo["transferEncoding"], "逐跳头不透传")
        self.assertEqual(gate.scheduler.stats["admitted"], 1)

    def test_upstream_status_passthrough(self) -> None:
        """上游的 4xx 原样回给客户端。"""
        up = FakeUpstream(self)

        def responder(handler: Any, _body: bytes) -> None:
            payload = b'{"type":"error","error":{"type":"invalid_request_error"}}'
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        up.responder = responder
        gate = GateFixture(self, up.url)
        status, _headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 400)
        self.assertIn(b"invalid_request_error", data)

    def test_chunked_request_body(self) -> None:
        """客户端用 chunked 发请求体：解码后按定长转给上游。"""
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url)

        conn = http.client.HTTPConnection("127.0.0.1", gate.port, timeout=10)
        try:
            conn.putrequest("POST", "/v1/messages")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"5\r\nhello\r\n")
            conn.send(b"6\r\n world\r\n")
            conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
            data = resp.read()
            self.assertEqual(resp.status, 200)
        finally:
            conn.close()

        echo = json.loads(data)
        self.assertEqual(echo["body"], "hello world")
        self.assertEqual(echo["contentLength"], "11")

    def test_head_without_body(self) -> None:
        """HEAD 不回响应体，也不会把连接读挂。"""
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url)
        status, _headers, data = http_request(gate.port, method="HEAD", path="/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"")

    def test_upstream_down_returns_502(self) -> None:
        """上游连不上：返回 502 而不是挂死。"""
        gate = GateFixture(self, f"http://127.0.0.1:{free_port()}")
        status, _headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 502)
        payload = json.loads(data)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "api_error")
        self.assertIn("[cc-rpm-gate]", payload["error"]["message"])


class TestStreaming(unittest.TestCase):
    def test_sse_not_buffered(self) -> None:
        """SSE 流式：分块陆续到达，不被攒成一坨。"""
        up = FakeUpstream(self)
        chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: content_block_delta\ndata: {\"text\":\"hi\"}\n\n",
            b"event: message_stop\ndata: {}\n\n",
        ]

        def responder(handler: Any, _body: bytes) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "private")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for payload in chunks:
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                handler.wfile.flush()
                time.sleep(0.06)
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()

        up.responder = responder
        gate = GateFixture(self, up.url)

        conn = http.client.HTTPConnection("127.0.0.1", gate.port, timeout=10)
        arrivals: List[float] = []
        received = b""
        try:
            conn.request("POST", "/v1/messages", body=b'{"stream":true}')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            headers = {k.lower(): v for k, v in resp.getheaders()}
            self.assertEqual(headers.get("content-type"), "text/event-stream")
            self.assertEqual(headers.get("x-accel-buffering"), "no", "必须禁用中间缓冲")
            self.assertEqual(headers.get("cache-control"), "no-cache, no-transform")
            self.assertNotIn("content-length", headers, "流式响应不能带 Content-Length")

            start = time.monotonic()
            while True:
                part = resp.read1(4096)
                if not part:
                    break
                arrivals.append(time.monotonic() - start)
                received += part
        finally:
            conn.close()

        self.assertEqual(received, b"".join(chunks), "分块内容拼起来要和上游一致")
        self.assertGreaterEqual(len(arrivals), 2, "至少分两次到达，说明没有整体缓冲")
        self.assertGreaterEqual(arrivals[-1] - arrivals[0], 0.05, "分块之间应保留上游的时间间隔")


class TestQueueing(unittest.TestCase):
    def test_max_concurrent_enforced(self) -> None:
        """排队生效：maxConcurrent=1 时上游同时只看到 1 个请求。"""
        up = FakeUpstream(self)

        def responder(handler: Any, _body: bytes) -> None:
            time.sleep(0.08)
            json_responder(handler, b"")

        up.responder = responder
        gate = GateFixture(self, up.url, maxConcurrent=1)

        results = parallel([lambda: http_request(gate.port, body=b"{}") for _ in range(4)])
        self.assertTrue(all(isinstance(r, tuple) and r[0] == 200 for r in results), results)
        self.assertEqual(up.state.peak_in_flight, 1, "上游同时只应看到 1 个请求")
        self.assertEqual(len(up.state.requests), 4)

    def test_window_throttle(self) -> None:
        """每 X 毫秒 Y 次：跨请求节流真的把请求摊开。"""
        up = FakeUpstream(self)
        gate = GateFixture(
            self,
            up.url,
            maxConcurrent=5,
            effectiveLimits=[{"windowMs": 200, "max": 2}],
        )

        results = parallel([lambda: http_request(gate.port, body=b"{}") for _ in range(4)])
        self.assertTrue(all(isinstance(r, tuple) and r[0] == 200 for r in results), results)

        hits = sorted(up.state.hits)
        self.assertEqual(len(hits), 4)
        # 前 2 个在同一个窗口，第 3 个必须等窗口滚出去
        self.assertGreaterEqual(hits[2] - hits[0], 0.18, "第 3 个请求应被节流到下一个窗口")

    def test_min_gap(self) -> None:
        """最小间隔：相邻两次放行至少隔 minGapMs。"""
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url, maxConcurrent=5, minGapMs=80)

        results = parallel([lambda: http_request(gate.port, body=b"{}") for _ in range(3)])
        self.assertTrue(all(isinstance(r, tuple) and r[0] == 200 for r in results), results)
        hits = sorted(up.state.hits)
        self.assertGreaterEqual(hits[1] - hits[0], 0.07)
        self.assertGreaterEqual(hits[2] - hits[1], 0.07)

    def test_bypass_paths(self) -> None:
        """旁路：count_tokens 和 GET 在槽位占满时也能立刻返回。"""
        up = FakeUpstream(self)
        hold = threading.Event()

        def responder(handler: Any, _body: bytes) -> None:
            if handler.path == "/v1/messages":
                hold.wait(timeout=5)
            json_responder(handler, b"")

        up.responder = responder
        gate = GateFixture(self, up.url, maxConcurrent=1)

        slow = threading.Thread(
            target=lambda: http_request(gate.port, body=b"{}"), daemon=True
        )
        slow.start()
        self.assertTrue(wait_until(lambda: gate.scheduler.in_flight == 1), "慢请求应已占住唯一槽位")

        started = time.monotonic()
        status_a, _h, _d = http_request(
            gate.port, method="POST", path="/v1/messages/count_tokens", body=b"{}"
        )
        status_b, _h, _d = http_request(gate.port, method="GET", path="/v1/models")
        elapsed = time.monotonic() - started

        self.assertEqual(status_a, 200, "count_tokens 不排队")
        self.assertEqual(status_b, 200, "GET 不排队")
        self.assertLess(elapsed, 2.0, "旁路请求不该被慢请求堵住")
        self.assertEqual(gate.scheduler.stats["admitted"], 1, "旁路请求不进队列")

        hold.set()
        slow.join(timeout=5)

    def test_queue_full_returns_503(self) -> None:
        """队列已满：返回 503 和 Anthropic 风格错误体。"""
        up = FakeUpstream(self)
        hold = threading.Event()

        def responder(handler: Any, _body: bytes) -> None:
            hold.wait(timeout=5)
            json_responder(handler, b"")

        up.responder = responder
        gate = GateFixture(self, up.url, maxConcurrent=1, maxQueue=1)

        results = parallel([lambda: http_request(gate.port, body=b"{}") for _ in range(4)])
        hold.set()

        statuses = [r[0] for r in results if isinstance(r, tuple)]
        rejected = [r for r in results if isinstance(r, tuple) and r[0] == 503]
        self.assertTrue(rejected, f"应至少有一个被拒，实际: {statuses}")

        _status, headers, data = rejected[0]
        self.assertEqual(headers.get("retry-after"), "5")
        payload = json.loads(data)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "overloaded_error")
        self.assertIn("[cc-rpm-gate]", payload["error"]["message"])
        self.assertIn("排队已满", payload["error"]["message"])
        self.assertGreaterEqual(gate.scheduler.stats["rejected"], 1)

    def test_queue_timeout_returns_504(self) -> None:
        """排队超时：返回 504。"""
        up = FakeUpstream(self)
        hold = threading.Event()

        def responder(handler: Any, _body: bytes) -> None:
            hold.wait(timeout=5)
            json_responder(handler, b"")

        up.responder = responder
        gate = GateFixture(self, up.url, maxConcurrent=1, queueTimeoutMs=150)

        slow = threading.Thread(target=lambda: http_request(gate.port, body=b"{}"), daemon=True)
        slow.start()
        self.assertTrue(wait_until(lambda: gate.scheduler.in_flight == 1))

        status, headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 504)
        self.assertEqual(headers.get("retry-after"), "5")
        payload = json.loads(data)
        self.assertEqual(payload["error"]["type"], "api_error")
        self.assertEqual(gate.scheduler.stats["timedOut"], 1)

        hold.set()
        slow.join(timeout=5)

    def test_client_abort_in_queue(self) -> None:
        """客户端排队中断开：出队并把名额让给下一个。"""
        up = FakeUpstream(self)
        hold = threading.Event()

        def responder(handler: Any, _body: bytes) -> None:
            if handler.path == "/v1/messages":
                hold.wait(timeout=5)
            json_responder(handler, b"")

        up.responder = responder
        gate = GateFixture(self, up.url, maxConcurrent=1)

        slow = threading.Thread(target=lambda: http_request(gate.port, body=b"{}"), daemon=True)
        slow.start()
        self.assertTrue(wait_until(lambda: gate.scheduler.in_flight == 1))

        # 发完请求就立刻关掉连接，模拟窗口被用户 Ctrl+C
        doomed = http.client.HTTPConnection("127.0.0.1", gate.port, timeout=5)
        doomed.request("POST", "/v1/messages", body=b"{}")
        self.assertTrue(wait_until(lambda: gate.scheduler.snapshot()["queued"] == 1))
        doomed.close()

        self.assertTrue(
            wait_until(lambda: gate.scheduler.stats["aborted"] == 1),
            "断开的请求应立刻出队",
        )
        self.assertEqual(gate.scheduler.snapshot()["queued"], 0)
        self.assertEqual(gate.scheduler.stats["admitted"], 1, "断开的请求没占名额")

        hold.set()
        slow.join(timeout=5)


class TestPenalty(unittest.TestCase):
    def _limited_gate(
        self, status: int, retry_after: Optional[str] = None, error_type: str = "rate_limit_error"
    ) -> Tuple[FakeUpstream, GateFixture]:
        up = FakeUpstream(self)

        def responder(handler: Any, _body: bytes) -> None:
            payload = ('{"type":"error","error":{"type":"%s"}}' % error_type).encode("utf-8")
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            if retry_after is not None:
                handler.send_header("Retry-After", retry_after)
            handler.end_headers()
            handler.wfile.write(payload)

        up.responder = responder
        return up, GateFixture(self, up.url, penaltyMs=100, maxPenaltyMs=400)

    def test_429_triggers_cooldown(self) -> None:
        """上游 429：透传给客户端，并触发全局冷却。"""
        _up, gate = self._limited_gate(429, retry_after="1")
        status, headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 429, "原样透传，让 Claude Code 自己重试")
        self.assertEqual(headers.get("retry-after"), "1")
        self.assertIn(b"rate_limit_error", data)

        self.assertEqual(gate.scheduler.stats["penalties"], 1)
        self.assertEqual(gate.scheduler.penalty_streak, 1)
        snap = gate.scheduler.snapshot()
        self.assertGreater(snap["penaltyMsLeft"], 0, "应进入冷却")
        self.assertLessEqual(snap["penaltyMsLeft"], 1000)

    def test_529_triggers_cooldown(self) -> None:
        """上游 529：同样触发冷却。"""
        _up, gate = self._limited_gate(529)
        status, _headers, _data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 529)
        self.assertEqual(gate.scheduler.stats["penalties"], 1)
        self.assertGreater(gate.scheduler.snapshot()["penaltyMsLeft"], 0)

    def test_503_passes_through_without_cooldown(self) -> None:
        """上游 503：原样透传但不进冷却（只有 429/529 算限流）。"""
        _up, gate = self._limited_gate(503, error_type="api_error")
        status, _headers, data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 503, "上游 5xx 原样回给客户端")
        self.assertIn(b"api_error", data)

        self.assertEqual(gate.scheduler.stats["admitted"], 1, "这个 503 来自上游，不是闸门自己挡的")
        self.assertEqual(gate.scheduler.stats["rejected"], 0)
        self.assertEqual(gate.scheduler.stats["penalties"], 0, "非 429/529 不触发冷却")
        self.assertEqual(gate.scheduler.penalty_streak, 0)
        self.assertEqual(gate.scheduler.snapshot()["penaltyMsLeft"], 0, "后续请求不该被罚等")

    def test_success_resets_streak(self) -> None:
        """正常响应后退避计数归零。"""
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url)
        gate.scheduler.penalize(0)
        self.assertEqual(gate.scheduler.penalty_streak, 1)
        status, _headers, _data = http_request(gate.port, body=b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(gate.scheduler.penalty_streak, 0)


class TestGateEndpoints(unittest.TestCase):
    def test_status(self) -> None:
        """状态接口：返回限流配置与实时用量。"""
        up = FakeUpstream(self)
        gate = GateFixture(
            self,
            up.url,
            maxConcurrent=3,
            minGapMs=7,
            effectiveLimits=[{"windowMs": 1000, "max": 5}, {"windowMs": 60000, "max": 20}],
        )
        self.assertEqual(http_request(gate.port, body=b"{}")[0], 200)

        status, headers, data = http_request(gate.port, method="GET", path="/__gate/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        snap = json.loads(data)
        self.assertEqual(snap["upstream"], up.url)
        self.assertEqual(snap["listen"], f"127.0.0.1:{gate.port}")
        self.assertEqual(snap["inFlight"], 0)
        self.assertEqual(snap["queued"], 0)
        self.assertEqual(snap["maxConcurrent"], 3)
        self.assertEqual(snap["minGapMs"], 7)
        self.assertEqual([l["windowMs"] for l in snap["limits"]], [1000, 60000])
        self.assertEqual(snap["limits"][0]["max"], 5)
        self.assertEqual(snap["limits"][0]["used"], 1)
        self.assertEqual(snap["stats"]["admitted"], 1)
        self.assertEqual(len(up.state.requests), 1, "状态接口不该转发到上游")

    def test_health(self) -> None:
        up = FakeUpstream(self)
        gate = GateFixture(self, up.url)
        status, _headers, data = http_request(gate.port, method="GET", path="/__gate/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True})
        self.assertEqual(len(up.state.requests), 0)


class TestHelpers(unittest.TestCase):
    def test_parse_retry_after(self) -> None:
        """parseRetryAfter：支持秒数和 HTTP 日期。"""
        self.assertEqual(parse_retry_after("2"), 2000)
        self.assertEqual(parse_retry_after("0"), 0)
        self.assertEqual(parse_retry_after(" 1.5 "), 1500)
        self.assertTrue(parse_retry_after(None) != parse_retry_after(None))  # nan
        self.assertTrue(parse_retry_after("later") != parse_retry_after("later"))  # nan

        future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
        ms = parse_retry_after(future)
        self.assertGreater(ms, 20000)
        self.assertLess(ms, 40000)

        past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 30))
        self.assertEqual(parse_retry_after(past), 0, "过去的时间按 0 算")

    def test_build_upstream_headers(self) -> None:
        """buildUpstreamHeaders：换掉 host 并剔除逐跳头和 Content-Length。"""
        out = build_upstream_headers(
            [
                ("Host", "127.0.0.1:17530"),
                ("Connection", "keep-alive"),
                ("Transfer-Encoding", "chunked"),
                ("Content-Length", "99"),
                ("Authorization", "Bearer x"),
                ("anthropic-version", "2023-06-01"),
            ],
            "127.0.0.1:17521",
        )
        keys = [k.lower() for k, _ in out]
        self.assertNotIn("connection", keys)
        self.assertNotIn("transfer-encoding", keys)
        self.assertNotIn("content-length", keys)
        self.assertEqual(keys.count("host"), 1)
        self.assertEqual(dict(out)["Host"], "127.0.0.1:17521")
        self.assertEqual(dict(out)["Authorization"], "Bearer x")
        self.assertEqual(dict(out)["anthropic-version"], "2023-06-01")

    def test_build_client_headers(self) -> None:
        out = build_client_headers([
            ("Content-Type", "application/json"),
            ("Connection", "close"),
            ("Keep-Alive", "timeout=5"),
            ("Content-Length", "12"),
            ("X-Request-Id", "req_1"),
        ])
        keys = [k.lower() for k, _ in out]
        self.assertEqual(keys, ["content-type", "x-request-id"])


if __name__ == "__main__":
    unittest.main()
