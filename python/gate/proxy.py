"""反向代理：把客户端请求原样转发到上游，中间插排队闸门。

基于标准库 http.server（每个连接一个线程）+ http.client，零第三方依赖。
关键点：响应体边读边写，SSE 不缓冲；排队期间客户端断开立刻出队。
与 Node 版的差异：请求体先完整读进内存再排队（Claude Code 的请求体有 Content-Length，体积可控）。
"""

from __future__ import annotations

import http.client
import json
import math
import select
import socket
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit

from .retry import request_overrides
from .scheduler import GateError, Scheduler

# 逐跳头部，不能透传给上游（也不能回给客户端）
HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
})

# 上游单次读写的 socket 超时（秒）。SSE 长时间没数据也会触发，所以取得足够宽松
UPSTREAM_TIMEOUT_S = 900
CHUNK_SIZE = 64 * 1024
# 请求体上限，防止异常请求把内存吃光；1M 上下文的请求也远小于这个值
MAX_BODY_BYTES = 256 * 1024 * 1024

CLIENT_GONE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# 被拦下来的限流响应体最多缓存多少：超过就不重试，直接边读边回
MAX_CAPTURE_BYTES = 256 * 1024
# 命名上游的访问前缀：/__gate/u/<名字>/v1/messages
UPSTREAM_PREFIX = "/__gate/u/"


def resolve_upstream(
    pathname: str,
    search: str,
    upstreams: Dict[str, str],
    fallback: str,
) -> Dict[str, Any]:
    """解析 /__gate/u/<名字>/... 前缀，挑出该走哪个上游。没有前缀就走默认 upstream。"""
    if not pathname.startswith(UPSTREAM_PREFIX):
        return {"target": fallback, "path": pathname + search, "name": ""}
    rest = pathname[len(UPSTREAM_PREFIX):]
    name, slash, tail = rest.partition("/")
    target = upstreams.get(name)
    if not target:
        return {"target": None, "path": "", "name": name}
    return {"target": target, "path": ("/" + tail if slash else "/") + search, "name": name}


class BodyTooLarge(ValueError):
    pass


def parse_retry_after(value: Any) -> float:
    """解析 Retry-After：既支持秒数，也支持 HTTP 日期；解析不了返回 nan。"""
    if not value:
        return math.nan
    text = str(value).strip()
    try:
        secs = float(text)
        if math.isfinite(secs):
            return max(0.0, secs * 1000)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return math.nan
    if when is None:
        return math.nan
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when.timestamp() - time.time()) * 1000)


def build_upstream_headers(items: Iterable[Tuple[str, str]], upstream_netloc: str) -> List[Tuple[str, str]]:
    """过滤逐跳头部和 Content-Length（由我们按实际请求体重算），并把 host 换成上游的。"""
    out = [
        (k, v) for k, v in items
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-length"
    ]
    out.append(("Host", upstream_netloc))
    return out


def build_client_headers(items: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """过滤逐跳头部和 Content-Length（由 _relay 决定用定长还是 chunked）。"""
    return [
        (k, v) for k, v in items
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-length"
    ]


def client_gone(sock: socket.socket) -> bool:
    """非阻塞探测客户端是否已经断开：可读且读到 0 字节就是对端关了。"""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _read_chunked(rfile: Any, limit: int) -> bytes:
    """解码 chunked 请求体。"""
    parts: List[bytes] = []
    total = 0
    while True:
        line = rfile.readline(65537)
        if not line:
            raise ConnectionResetError("chunked 请求体没读完客户端就断开了")
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise ValueError(f"chunk 长度非法: {size_text!r}") from None
        if size == 0:
            # 读掉 trailer，直到空行
            while True:
                trailer = rfile.readline(65537)
                if trailer in (b"\r\n", b"\n", b""):
                    return b"".join(parts)
        total += size
        if total > limit:
            raise BodyTooLarge(f"请求体超过 {limit} 字节")
        chunk = rfile.read(size)
        if len(chunk) < size:
            raise ConnectionResetError("chunked 请求体没读完客户端就断开了")
        parts.append(chunk)
        rfile.readline(65537)  # 每个 chunk 后面的 CRLF


def make_handler_class(
    cfg: Dict[str, Any],
    scheduler: Scheduler,
    logger: Any,
    retry_hook: Any = None,
) -> type:
    """按配置生成请求处理类（http.server 要的是类，不是实例）。"""
    bypass = frozenset(cfg["bypassPaths"])
    upstreams: Dict[str, str] = cfg.get("upstreams") or {}

    def parse_target(url: str) -> Dict[str, Any]:
        """把上游 URL 拆成 http.client 需要的几件东西。"""
        parts = urlsplit(url)
        is_https = parts.scheme == "https"
        return {
            "host": parts.hostname,
            "port": parts.port or (443 if is_https else 80),
            "netloc": parts.netloc,
            "basePath": parts.path.rstrip("/"),
            "connCls": http.client.HTTPSConnection if is_https else http.client.HTTPConnection,
        }

    class GateHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # 支持 keep-alive 和 chunked
        server_version = "cc-rpm-gate"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # 关掉 http.server 自带的 stderr 访问日志，统一走 logger
            logger.debug("access " + (format % args))

        def do_GET(self) -> None:
            self._handle()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

        # ---------- 主流程 ----------

        def _handle(self) -> None:
            raw_path = self.path if self.path.startswith("/") else self._origin_form(self.path)
            pathname = raw_path.split("?", 1)[0] or "/"

            # 闸门自身的状态接口，不走队列
            if pathname == "/__gate/status":
                self._send_json(200, {
                    "upstream": cfg["upstream"],
                    "upstreams": dict(upstreams),
                    "listen": f"{cfg['host']}:{cfg['port']}",
                    **scheduler.snapshot(),
                    "retry": retry_hook.snapshot() if retry_hook is not None else {"enabled": False},
                })
                return
            if pathname == "/__gate/health":
                self._send_json(200, {"ok": True})
                return

            # 命名上游：/__gate/u/<名字>/... → 走 cfg["upstreams"][名字]
            _p, qmark, query = raw_path.partition("?")
            route = resolve_upstream(pathname, ("?" + query) if qmark else "", upstreams, cfg["upstream"])
            if route["target"] is None:
                self._send_gate_error(
                    404, "invalid_request_error",
                    f"未配置名为 {route['name']} 的上游，请在 gate.config.json 的 upstreams 里加上")
                return
            target = parse_target(route["target"])
            # 排队/旁路判断按真实业务路径走，不带 /__gate/u/<名字> 前缀
            real_path = route["path"].split("?", 1)[0] or "/"
            meta = {"window": self._window_id(), "path": real_path, "method": self.command}
            if route["name"]:
                meta["upstream"] = route["name"]

            try:
                body = self._read_body()
            except BodyTooLarge as err:
                self.close_connection = True
                self._send_gate_error(413, "request_too_large", str(err))
                return
            except ValueError as err:
                self.close_connection = True
                self._send_gate_error(400, "invalid_request_error", f"请求体不合法: {err}")
                return
            except CLIENT_GONE_ERRORS + (socket.timeout,):
                self.close_connection = True
                return

            if self.command != "POST" or real_path in bypass:
                logger.debug("直放（不排队）", meta)
                self._forward(target, route["path"], body, meta)
                return

            overrides = request_overrides(self.headers) if retry_hook is not None else {}
            can_retry = (
                retry_hook is not None
                and retry_hook.cfg["enabled"]
                and retry_hook.max_retries_for(overrides) > 0
            )
            intercept = list(retry_hook.cfg["statuses"]) if can_retry else []

            attempt = 0
            held_ms = 0.0            # 这个请求累计被暂存了多久
            last_captured = None     # 最近一次被拦下来的上游限流响应
            stash_release_at = None  # 正在暂存时的预计放行时刻
            acquire_kwargs: Dict[str, Any] = {}

            while True:
                try:
                    # 排队期间客户端断开，立刻出队把名额让给别人
                    slot = scheduler.acquire(
                        meta,
                        cancel_check=lambda: client_gone(self.connection),
                        **acquire_kwargs,
                    )
                except GateError as err:
                    if stash_release_at is not None:
                        retry_hook.leave(stash_release_at, 0)
                        stash_release_at = None
                    if err.code == "CLIENT_ABORTED":
                        self.close_connection = True
                        return
                    # 暂存重试没能重新排上：把上游原来的限流响应回给客户端，比回闸门自己的错有用
                    if last_captured is not None:
                        self._send_captured(last_captured)
                        return
                    etype = "api_error" if err.status == 504 else "overloaded_error"
                    self._send_gate_error(err.status, etype, err.message, {"Retry-After": "5"})
                    return

                if stash_release_at is not None:
                    retry_hook.leave(stash_release_at, slot.waited_ms)
                    stash_release_at = None
                if slot.waited_ms > 1000:
                    logger.info("排队后放行",
                                {**meta, "waitedMs": round(slot.waited_ms), "ticket": slot.ticket})

                attempt += 1
                if attempt > 1:
                    retry_hook.record_retry()

                try:
                    result = self._forward(
                        target,
                        route["path"],
                        body,
                        {**meta, "ticket": slot.ticket, "attempt": attempt},
                        # 被拦下来的状态码在下面单独处理，其余仍然「先更新冷却再回包」，
                        # 否则客户端收到 529 可能抢在冷却生效前就重试
                        on_status=lambda s, retry_ms: (
                            None if s in intercept else self._update_penalty(s, retry_ms, meta)
                        ),
                        intercept=intercept,
                    )
                except BaseException:
                    slot.release()
                    raise

                status = result.get("status", 502)

                # 没被拦下来：响应已经原样回给客户端了
                if status not in intercept:
                    # 冷却/成功计数已经在 on_status 里（回包之前）更新过了
                    if intercept and status < 500 and attempt > 1:
                        retry_hook.record_recovered()
                    slot.release()
                    return

                # 被拦下来的限流响应：问钩子是重试还是透传
                if result.get("captured") is not None:
                    last_captured = result["captured"]
                decision = retry_hook.decide(
                    status=status,
                    attempt=attempt,
                    held_ms=held_ms,
                    retry_after_ms=result.get("retryAfterMs"),
                    overrides=overrides,
                )

                if decision["action"] == "retry" and last_captured is not None:
                    # 冷却按暂存时长精确设置：这段时间里所有窗口一起等，别人别去白跑
                    scheduler.penalize(result.get("retryAfterMs"), exact_ms=decision["waitMs"])
                    stash_release_at = retry_hook.enter(decision["waitMs"])
                    held_ms += decision["waitMs"]
                    logger.warn("上游限流，暂存后重试", {
                        **meta,
                        "status": status,
                        "mode": decision["mode"],
                        "waitMs": round(decision["waitMs"]),
                        "attempt": attempt,
                        "stashed": retry_hook.stashed,
                    })
                    slot.release()
                    # 插队重排：冷却到点后由调度器按 minGap/限流窗口逐个放行，不会一起炸出去
                    acquire_kwargs = {
                        "priority": True,
                        "timeout_ms": decision["waitMs"] + max(cfg["queueTimeoutMs"], 30000),
                    }
                    continue

                # 不重试了：把上游的限流响应原样交给客户端，让它自己退避
                waited = scheduler.penalize(result.get("retryAfterMs"))
                logger.warn("上游返回限流状态（透传）", {
                    **meta,
                    "status": status,
                    "cooldownMs": waited,
                    "attempt": attempt,
                    "reason": decision.get("reason"),
                })
                slot.release()
                if last_captured is not None:
                    self._send_captured(last_captured)
                else:
                    self._send_gate_error(
                        status, "rate_limit_error",
                        f"上游返回 {status}，且响应体过大无法暂存", {"Retry-After": "5"})
                return

        def _update_penalty(self, status: int, retry_after_ms: float, meta: Dict[str, Any]) -> None:
            """上游状态一出来就更新冷却，必须发生在回包之前，否则客户端可能抢先重试。"""
            # 上游限流：原样透传让客户端自己退避，同时所有窗口一起进冷却
            if status in (429, 529):
                waited = scheduler.penalize(retry_after_ms)
                logger.warn("上游返回限流状态", {**meta, "status": status, "cooldownMs": waited})
            elif status < 500:
                scheduler.on_success()

        # ---------- 转发 ----------

        def _forward(
            self,
            target: Dict[str, Any],
            raw_path: str,
            body: bytes,
            meta: Dict[str, Any],
            on_status: Any = None,
            intercept: Any = (),
        ) -> Dict[str, Any]:
            url = f"{target['netloc']}"
            conn = target["connCls"](target["host"], target["port"], timeout=UPSTREAM_TIMEOUT_S)
            try:
                try:
                    conn.putrequest(self.command, target["basePath"] + raw_path,
                                    skip_host=True, skip_accept_encoding=True)
                    for key, value in build_upstream_headers(self.headers.items(), target["netloc"]):
                        conn.putheader(key, value)
                    if body or self.command in ("POST", "PUT", "PATCH"):
                        conn.putheader("Content-Length", str(len(body)))
                    conn.endheaders(body if body else None)
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as err:
                    logger.error("转发到上游失败", {**meta, "error": str(err), "upstream": url})
                    self._send_gate_error(502, "api_error", f"无法连接上游 {url}: {err}")
                    return {"status": 502, "error": str(err)}
                return self._relay(resp, meta, on_status, intercept)
            finally:
                conn.close()

        def _relay(
            self,
            resp: http.client.HTTPResponse,
            meta: Dict[str, Any],
            on_status: Any = None,
            intercept: Any = (),
        ) -> Dict[str, Any]:
            """把上游响应边读边写回客户端。"""
            status = resp.status

            # 要拦下来的限流响应：不写客户端，整个收进内存，交给调用方决定重试还是回包
            if status in intercept:
                retry_ms = parse_retry_after(resp.getheader("Retry-After"))
                parts: List[bytes] = []
                size = 0
                overflow = False
                try:
                    while True:
                        chunk = resp.read1(CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_CAPTURE_BYTES:
                            overflow = True
                            continue
                        parts.append(chunk)
                except (OSError, http.client.HTTPException) as err:
                    logger.warn("上游限流响应读取中断", {**meta, "error": str(err)})
                    return {"status": status, "retryAfterMs": retry_ms, "error": str(err)}
                captured = None if overflow else {
                    "status": status,
                    "headers": build_client_headers(resp.getheaders()),
                    "body": b"".join(parts),
                }
                return {"status": status, "retryAfterMs": retry_ms, "captured": captured}

            headers = build_client_headers(resp.getheaders())
            # SSE 必须禁用中间缓冲，否则流式输出会攒成一坨
            if "text/event-stream" in (resp.getheader("Content-Type") or ""):
                headers = [(k, v) for k, v in headers if k.lower() != "cache-control"]
                headers += [("Cache-Control", "no-cache, no-transform"), ("X-Accel-Buffering", "no")]

            no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
            length = resp.getheader("Content-Length")
            upstream_chunked = "chunked" in (resp.getheader("Transfer-Encoding") or "").lower()
            use_chunked = False
            if length is not None and not upstream_chunked:
                headers.append(("Content-Length", length))
            elif not no_body:
                # 上游是 chunked 或靠关连接结束：统一用 chunked 回给客户端
                headers.append(("Transfer-Encoding", "chunked"))
                use_chunked = True

            retry_after_ms = parse_retry_after(resp.getheader("Retry-After"))
            # 先更新冷却再回包：否则客户端收到 429 立刻重试时，冷却可能还没生效
            if on_status is not None:
                on_status(status, retry_after_ms)

            try:
                self.send_response_only(status, resp.reason or None)
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
            except CLIENT_GONE_ERRORS:
                return self._client_aborted(meta)

            if no_body:
                return {"status": status, "retryAfterMs": retry_after_ms}

            while True:
                try:
                    chunk = resp.read1(CHUNK_SIZE)
                except (OSError, http.client.HTTPException) as err:
                    # 头已经发出去了，只能掐断连接让客户端感知
                    logger.warn("上游响应中断", {**meta, "error": str(err)})
                    self.close_connection = True
                    return {"status": status, "error": str(err)}
                try:
                    if not chunk:
                        if use_chunked:
                            self.wfile.write(b"0\r\n\r\n")
                        break
                    if use_chunked:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    else:
                        self.wfile.write(chunk)
                except CLIENT_GONE_ERRORS:
                    return self._client_aborted(meta)

            return {"status": status, "retryAfterMs": retry_after_ms}

        def _client_aborted(self, meta: Dict[str, Any]) -> Dict[str, Any]:
            # 客户端中途断开：直接返回，finally 里关掉上游连接，别让它白跑
            self.close_connection = True
            logger.debug("客户端在响应途中断开", meta)
            return {"status": 499, "aborted": True}

        # ---------- 工具 ----------

        def _read_body(self) -> bytes:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return _read_chunked(self.rfile, MAX_BODY_BYTES)
            length = self.headers.get("Content-Length")
            if length is None:
                return b""
            try:
                n = int(length)
            except ValueError:
                raise ValueError(f"Content-Length 非法: {length}") from None
            if n < 0:
                raise ValueError(f"Content-Length 非法: {length}")
            if n > MAX_BODY_BYTES:
                raise BodyTooLarge(f"请求体超过 {MAX_BODY_BYTES} 字节")
            data = self.rfile.read(n)
            if len(data) < n:
                raise ConnectionResetError("请求体没读完客户端就断开了")
            return data

        @staticmethod
        def _origin_form(path: str) -> str:
            # 绝对形式的 URL（http://host/path）只取 path + query
            parts = urlsplit(path)
            return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        def _window_id(self) -> str:
            """从请求头里猜一个窗口标识，仅用于日志区分是哪个 Claude Code 窗口。"""
            for name in ("x-gate-window", "x-session-id", "x-claude-session-id", "x-request-id"):
                value = self.headers.get(name)
                if value:
                    return value
            host, port = self.client_address[:2]
            return f"{host}:{port}"

        def _send_bytes(self, status: int, payload: bytes, extra: Dict[str, str] | None = None) -> None:
            try:
                self.send_response_only(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except CLIENT_GONE_ERRORS:
                self.close_connection = True

        def _send_captured(self, captured: Dict[str, Any]) -> None:
            """把之前拦下来的上游响应原样回给客户端。"""
            payload = captured["body"]
            try:
                self.send_response_only(captured["status"])
                for key, value in captured["headers"]:
                    if key.lower() == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except CLIENT_GONE_ERRORS:
                self.close_connection = True

        def _send_json(self, status: int, payload: Any) -> None:
            body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self._send_bytes(status, body)

        def _send_gate_error(self, status: int, etype: str, message: str, extra: Dict[str, str] | None = None) -> None:
            """按 Anthropic 的错误体格式回错，Claude Code 能正常解析并重试。"""
            body = json.dumps(
                {"type": "error", "error": {"type": etype, "message": f"[cc-rpm-gate] {message}"}},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            self._send_bytes(status, body, extra)

    return GateHandler
