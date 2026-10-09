"""用户重试钩子测试（与 Node 版 test/hooks.test.js 对应）。

覆盖：加载/热重载、返回值校验、与配置和请求头的优先级、端到端改重试次数。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from gate.config import DEFAULT_RETRY, ConfigError, load_config, normalize_retry
from gate.hooks import UserHook, create_user_hook, resolve_hook_path
from gate.logger import NullLogger
from gate.retry import RetryHook

try:
    from .test_proxy import http_request, make_cfg
    from .test_retry_proxy import LimitedUpstream
except ImportError:  # pragma: no cover - 取决于启动方式
    from test_proxy import http_request, make_cfg
    from test_retry_proxy import LimitedUpstream


class _TempHooks:
    """临时目录里写钩子文件；每次写都把 mtime 往后推，保证热重载能看到变化。"""

    def __init__(self, test: unittest.TestCase) -> None:
        self.dir = tempfile.mkdtemp(prefix="gate-hooks-")
        self._seq = 0
        test.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name: str, code: str) -> str:
        path = os.path.join(self.dir, name)
        Path(path).write_text(code, encoding="utf-8")
        self._seq += 1
        future = time.time() + self._seq
        os.utime(path, (future, future))
        return path


def make_hook(user_hook: Any = None, **retry: Any) -> RetryHook:
    return RetryHook({**DEFAULT_RETRY, **retry}, logger=NullLogger(),
                     wall_now_ms=lambda: 0.0, user_hook=user_hook)


class TestResolveAndCreate(unittest.TestCase):
    def test_resolve_hook_path(self) -> None:
        """补扩展名、认相对路径、别的语言不管。"""
        root = Path(tempfile.gettempdir(), "root")
        self.assertEqual(resolve_hook_path("", root), "", "空字符串 = 不启用")
        self.assertEqual(resolve_hook_path(False, root), "")
        self.assertEqual(resolve_hook_path(None, root), "")
        self.assertEqual(resolve_hook_path("gate.hooks", root), str((root / "gate.hooks.py").resolve()),
                         "没 py 扩展名就补 .py，且不能把 gate.hooks 变成 gate.py")
        self.assertEqual(resolve_hook_path("myhook", root), str((root / "myhook.py").resolve()))
        self.assertEqual(resolve_hook_path("a/b.py", root), str((root / "a" / "b.py").resolve()))
        self.assertEqual(resolve_hook_path("gate.hooks.js", root), "", ".js 交给 Node 版，这边不认")

    def test_no_hook_when_default_file_missing(self) -> None:
        tmp = _TempHooks(self)
        hook = create_user_hook({"retry": dict(DEFAULT_RETRY)}, logger=NullLogger(), root=tmp.dir)
        self.assertFalse(hook.enabled)
        self.assertEqual(hook.apply({"status": 429}), {}, "不启用时调用是空操作")

    def test_default_file_auto_loaded(self) -> None:
        tmp = _TempHooks(self)
        tmp.write("gate.hooks.py", "def on_retry(ctx):\n    return {'maxRetries': 7}\n")
        hook = create_user_hook({"retry": dict(DEFAULT_RETRY)}, logger=NullLogger(), root=tmp.dir)
        self.assertTrue(hook.enabled)
        self.assertTrue(hook.snapshot()["loaded"])
        self.assertEqual(hook.apply({"status": 429}), {"overrides": {"maxRetries": 7}})


class TestDecideWithHook(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _TempHooks(self)

    def user(self, name: str, code: str) -> UserHook:
        return UserHook(self.tmp.write(name, code), logger=NullLogger())

    def test_hook_raises_zero_retries(self) -> None:
        """配置 0 次，钩子按路径调到 3 次。"""
        u = self.user("h.py", "def on_retry(ctx):\n"
                              "    return {'maxRetries': 3} if ctx['path'] == '/v1/messages' else None\n")
        h = make_hook(u, maxRetries=0, cooldownMs=1000)
        self.assertTrue(h.has_user_hook(), "配了钩子就要提前缓存请求体")
        self.assertEqual(h.decide(status=429, attempt=1, meta={"path": "/v1/messages"}),
                         {"action": "retry", "waitMs": 1000, "mode": "cooldown"})
        no = h.decide(status=429, attempt=1, meta={"path": "/v1/other"})
        self.assertEqual(no["reason"], "retry-disabled", "钩子不干预时仍按配置的 0 次透传")

    def test_precedence(self) -> None:
        """钩子 > 请求头 > 配置。"""
        u = self.user("h.py", "def on_retry(ctx):\n    return {'maxRetries': 9, 'mode': 'nextMinute'}\n")
        h = make_hook(u, maxRetries=1, mode="cooldown", nextMinuteOffsetMs=1000)
        d = h.decide(status=429, attempt=3, overrides={"maxRetries": 2, "mode": "cooldown"})
        self.assertEqual(d["action"], "retry", "第 3 次仍允许：钩子把上限提到 9")
        self.assertEqual(d["mode"], "nextMinute")
        self.assertEqual(d["waitMs"], 1000, "wallNow=0（正好 :00）→ 等本分钟的 :01")

        quiet = make_hook(self.user("q.py", "def on_retry(ctx):\n    return None\n"), maxRetries=1)
        self.assertEqual(quiet.decide(status=429, attempt=2, overrides={"maxRetries": 2})["action"], "retry")
        self.assertEqual(quiet.decide(status=429, attempt=3, overrides={"maxRetries": 2})["reason"],
                         "max-retries")

    def test_deny(self) -> None:
        """返回 False / {'retry': False} 强制透传。"""
        for i, code in enumerate(["def on_retry(ctx):\n    return False\n",
                                  "def on_retry(ctx):\n    return {'retry': False}\n"]):
            u = self.user("deny%d.py" % i, code)
            h = make_hook(u, maxRetries=5)
            d = h.decide(status=429, attempt=1)
            self.assertEqual(d, {"action": "passthrough", "reason": "hook"})
            self.assertEqual(u.stats["forcedPassthrough"], 1)
            self.assertEqual(h.stats["exhausted"], 0, "钩子拦下的不算次数用尽")

    def test_wait_ms_forced(self) -> None:
        u = self.user("h.py", "def on_retry(ctx):\n    return {'waitMs': 2500}\n")
        h = make_hook(u, maxRetries=5, cooldownMs=1000, maxHoldMs=10000)
        self.assertEqual(h.decide(status=429, attempt=1, retry_after_ms=90000)["waitMs"], 2500)

    def test_cannot_change_statuses(self) -> None:
        u = self.user("h.py", "def on_retry(ctx):\n    return {'maxRetries': 5}\n")
        h = make_hook(u, maxRetries=2, statuses=[429])
        self.assertEqual(h.decide(status=503, attempt=1)["reason"], "not-retryable")
        self.assertEqual(u.stats["calls"], 0, "非限流状态码不该调钩子")

    def test_errors_and_bad_values_fall_back(self) -> None:
        """抛错 / 非法值：记一笔，然后按配置走。"""
        bad = self.user("throw.py", "def on_retry(ctx):\n    raise RuntimeError('boom')\n")
        h = make_hook(bad, maxRetries=2, cooldownMs=1000)
        self.assertEqual(h.decide(status=429, attempt=1),
                         {"action": "retry", "waitMs": 1000, "mode": "cooldown"})
        self.assertEqual(bad.stats["errors"], 1)
        self.assertEqual(bad.last_error, "boom")

        mixed = self.user("mixed.py", "def on_retry(ctx):\n"
                                      "    return {'maxRetries': -1, 'mode': 'whatever', 'cooldownMs': 2000,"
                                      " 'respectRetryAfter': 'yes'}\n")
        h2 = make_hook(mixed, maxRetries=2, mode="cooldown", cooldownMs=1000)
        d2 = h2.decide(status=429, attempt=1)
        self.assertEqual(d2["waitMs"], 2000, "合法的 cooldownMs 生效")
        self.assertEqual(d2["mode"], "cooldown", "非法 mode 被忽略")
        self.assertEqual(d2["action"], "retry", "非法 maxRetries 被忽略，用配置的 2")

        # bool 不算整数：True 不能当成 maxRetries=1
        boolish = self.user("b.py", "def on_retry(ctx):\n    return {'maxRetries': True}\n")
        h3 = make_hook(boolish, maxRetries=0)
        self.assertEqual(h3.decide(status=429, attempt=1)["reason"], "retry-disabled")

    def test_hot_reload(self) -> None:
        """改文件下一次就生效，坏文件保留上一个好版本。"""
        path = self.tmp.write("h.py", "def on_retry(ctx):\n    return {'maxRetries': 2}\n")
        u = UserHook(path, logger=NullLogger())
        u.reload_if_changed()
        self.assertEqual(u.apply({"status": 429}), {"overrides": {"maxRetries": 2}})
        self.assertEqual(u.stats["reloads"], 1)

        self.tmp.write("h.py", "def on_retry(ctx):\n    return {'maxRetries': 8}\n")
        self.assertEqual(u.apply({"status": 429}), {"overrides": {"maxRetries": 8}}, "改完不用重启")
        self.assertEqual(u.stats["reloads"], 2)

        self.tmp.write("h.py", "def on_retry(ctx):\n    return {'maxRetries':\n")
        self.assertEqual(u.apply({"status": 429}), {"overrides": {"maxRetries": 8}}, "坏文件不让闸门失灵")
        self.assertEqual(u.stats["errors"], 1)
        self.assertIsNotNone(u.snapshot()["lastError"])

        self.tmp.write("h.py", "def on_retry(ctx):\n    return {'maxRetries': 4}\n")
        self.assertEqual(u.apply({"status": 429}), {"overrides": {"maxRetries": 4}})

    def test_missing_on_retry(self) -> None:
        u = self.user("h.py", "something_else = 1\n")
        u.reload_if_changed()
        self.assertFalse(u.snapshot()["loaded"])
        self.assertIn("on_retry", u.last_error or "")
        self.assertEqual(u.apply({"status": 429}), {})

    def test_ctx_contents(self) -> None:
        """ctx 带请求信息和本次生效值，键名与 Node 版一致。"""
        seen: Dict[str, Any] = {}
        u = UserHook("", logger=NullLogger())
        # 直接塞一个函数进去，不经文件：只测 ctx 的组装
        u.file = "<inline>"
        u.fn = lambda ctx: seen.update(ctx)
        u.reload_if_changed = lambda: None  # type: ignore[assignment]
        h = make_hook(u, maxRetries=2, mode="cooldown", cooldownMs=1000)
        h.decide(status=429, attempt=2, held_ms=1500, retry_after_ms=3000,
                 overrides={"maxRetries": 4},
                 meta={"method": "POST", "path": "/v1/messages", "upstream": "myprovider", "window": "cc-1"})
        self.assertEqual(seen["status"], 429)
        self.assertEqual(seen["attempt"], 2)
        self.assertEqual(seen["heldMs"], 1500)
        self.assertEqual(seen["retryAfterMs"], 3000)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["upstream"], "myprovider")
        self.assertEqual(seen["window"], "cc-1")
        self.assertEqual(seen["stashed"], 0)
        self.assertEqual(seen["maxRetries"], 4, "ctx 里是请求头覆盖后的生效值")
        self.assertEqual(seen["mode"], "cooldown")
        self.assertTrue(seen["respectRetryAfter"])

    def test_snapshot(self) -> None:
        u = self.user("h.py", "def on_retry(ctx):\n    return {'maxRetries': 3}\n")
        h = make_hook(u, maxRetries=1)
        h.decide(status=429, attempt=1)
        snap = h.snapshot()["hook"]
        self.assertTrue(snap["enabled"])
        self.assertTrue(snap["loaded"])
        self.assertEqual(snap["stats"]["calls"], 1)
        self.assertEqual(snap["stats"]["overrides"], 1)
        self.assertIsNone(snap["lastError"])
        self.assertEqual(make_hook(None).snapshot()["hook"], {"enabled": False})

    def test_concurrent_calls(self) -> None:
        """多线程同时调钩子，计数不丢。"""
        u = self.user("h.py", "def on_retry(ctx):\n    return {'maxRetries': 3}\n")
        h = make_hook(u, maxRetries=1, maxStashed=1000)
        threads = [threading.Thread(target=lambda: [h.decide(status=429, attempt=1) for _ in range(50)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(u.stats["calls"], 400)
        self.assertEqual(u.stats["reloads"], 1, "文件没变就不该重复加载")


class TestHookConfig(unittest.TestCase):
    def test_retry_hook_config_and_env(self) -> None:
        self.assertEqual(normalize_retry({})["hook"], "")
        self.assertEqual(normalize_retry({"hook": "  my.py  "})["hook"], "my.py")
        self.assertEqual(normalize_retry({"hook": False})["hook"], "")
        with self.assertRaises(ConfigError):
            normalize_retry({"hook": 42})
        cfg = load_config(file=None, env={"GATE_RETRY_HOOK": "env.py"})
        self.assertEqual(cfg["retry"]["hook"], "env.py")


class TestHookEndToEnd(unittest.TestCase):
    def test_hook_turns_on_retries(self) -> None:
        """配置 0 次重试，钩子调到 2 次，客户端只看到 200。"""
        from gate.proxy import make_handler_class
        from gate.server import build

        tmp = _TempHooks(self)
        path = tmp.write("h.py", "def on_retry(ctx):\n    return {'maxRetries': 2, 'waitMs': 20}\n")
        up = LimitedUpstream(self, 2)

        cfg = make_cfg(up.url)
        cfg["retry"] = {**DEFAULT_RETRY, "maxRetries": 0, "maxHoldMs": 5000}
        server, scheduler, _log, cfg = build(cfg, NullLogger(), with_retry=False)
        retry_hook = RetryHook(cfg["retry"], logger=NullLogger(),
                               user_hook=UserHook(path, logger=NullLogger()))
        server.RequestHandlerClass = make_handler_class(cfg, scheduler, NullLogger(), retry_hook)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def close() -> None:
            scheduler.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)

        payload = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
        status, _h, data = http_request(cfg["port"], body=payload)
        self.assertEqual(status, 200, "客户端看不到 429")
        self.assertEqual(json.loads(data)["attempt"], 3)
        self.assertEqual(up.hits, 3, "1 次首发 + 2 次重试")
        self.assertEqual(up.bodies, [payload.decode()] * 3, "重放的请求体和原始一致")
        self.assertEqual(retry_hook.snapshot()["stats"]["retried"], 2)


if __name__ == "__main__":
    unittest.main()
