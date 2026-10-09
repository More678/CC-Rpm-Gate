"""UI 版背后的运行时测试：进程内起停、改配置即时生效、表单与配置互转、写回配置文件。

窗口本身（tkinter）不在这里测：CI 上没有显示器。界面只是把这些函数接到按钮上。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from gate.config import DEFAULT_RETRY, ConfigError, load_config
from gate.logger import Logger, NullLogger
from gate.runtime import GateRuntime, config_to_json, describe_changes, save_config
from gate.ui import FIELDS, config_to_form, form_to_config, status_lines

try:
    from .test_proxy import FakeUpstream, http_request, make_cfg
    from .test_retry_proxy import LimitedUpstream
except ImportError:  # pragma: no cover - 取决于启动方式
    from test_proxy import FakeUpstream, http_request, make_cfg
    from test_retry_proxy import LimitedUpstream


def runtime_cfg(upstream: str, **overrides: Any) -> Dict[str, Any]:
    cfg = make_cfg(upstream, **overrides)
    cfg.setdefault("retry", {**DEFAULT_RETRY, "cooldownMs": 30, "maxHoldMs": 5000})
    return cfg


class TestFormRoundTrip(unittest.TestCase):
    def test_every_field_round_trips(self) -> None:
        """配置 → 表单 → 配置，所有字段都原样回来。"""
        cfg = load_config(file=None, env={}, overrides={
            "rpm": 7, "limits": "5/10s, 2/1s, 30/2m",
            "upstreams": {"myprovider": "https://api.example.com"},
            "bypassPaths": ["/a", "/b"],
            "retry": {**DEFAULT_RETRY, "maxRetries": 4, "mode": "nextMinute", "hook": "my.py",
                      "statuses": [429, 503]},
        })
        back = form_to_config(config_to_form(cfg))
        for key in ("port", "host", "upstream", "rpm", "windowMs", "maxConcurrent", "minGapMs",
                    "maxQueue", "queueTimeoutMs", "penaltyMs", "maxPenaltyMs", "bypassPaths",
                    "upstreams", "logFile", "logLevel", "effectiveLimits"):
            self.assertEqual(back[key], cfg[key], key)
        self.assertEqual(sorted(back["limits"], key=lambda l: l["windowMs"]),
                         sorted(cfg["limits"], key=lambda l: l["windowMs"]))
        self.assertEqual(back["retry"], cfg["retry"])

    def test_form_uses_compact_limit_syntax(self) -> None:
        cfg = load_config(file=None, env={}, overrides={"limits": [{"seconds": 10, "max": 5},
                                                                    {"windowMs": 1500, "max": 3}]})
        self.assertEqual(config_to_form(cfg)["limits"], "5/10s, 3/1500ms")

    def test_form_validation_messages(self) -> None:
        form = config_to_form(load_config(file=None, env={}))
        bad = dict(form, **{"retry.maxRetries": "abc"})
        with self.assertRaisesRegex(ConfigError, "重试次数 必须是整数"):
            form_to_config(bad)
        with self.assertRaisesRegex(ConfigError, "监听端口 不能留空"):
            form_to_config(dict(form, port=""))
        with self.assertRaisesRegex(ConfigError, "命名上游 不是合法 JSON"):
            form_to_config(dict(form, upstreams="{oops"))
        with self.assertRaises(ConfigError):
            form_to_config(dict(form, **{"retry.mode": "sometimes"}))

    def test_form_ignores_environment(self) -> None:
        """界面上显示的就是生效值：环境变量不能在背后改掉它。"""
        import os
        form = config_to_form(load_config(file=None, env={}))
        form["retry.maxRetries"] = "3"
        old = os.environ.get("GATE_MAX_RETRIES")
        os.environ["GATE_MAX_RETRIES"] = "9"
        try:
            self.assertEqual(form_to_config(form)["retry"]["maxRetries"], 3)
        finally:
            if old is None:
                os.environ.pop("GATE_MAX_RETRIES", None)
            else:
                os.environ["GATE_MAX_RETRIES"] = old

    def test_fields_cover_all_retry_keys(self) -> None:
        """retry 里每个配置项界面上都能改，以后加字段忘了加到界面会在这里报出来。"""
        keys = {key.split(".", 1)[1] for _g, key, _l, _k, _h in FIELDS if key.startswith("retry.")}
        self.assertEqual(keys, set(DEFAULT_RETRY))


class TestSaveConfig(unittest.TestCase):
    def test_save_round_trips_through_load(self) -> None:
        tmp = tempfile.mkdtemp(prefix="gate-save-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp, "gate.config.json")
        cfg = load_config(file=None, env={}, overrides={
            "rpm": 11, "limits": "4/10s", "retry": {**DEFAULT_RETRY, "maxRetries": 6}})
        save_config(cfg, path)

        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("effectiveLimits", raw, "算出来的字段不写回")
        self.assertFalse(Path(raw["logFile"]).is_absolute(), "日志路径写回相对路径")
        self.assertTrue(raw["logFile"].startswith("logs/"))
        self.assertEqual(list(raw)[:3], ["port", "host", "upstream"], "键顺序和仓库里那份一致")

        again = load_config(file=path, env={})
        self.assertEqual(again["rpm"], 11)
        self.assertEqual(again["retry"]["maxRetries"], 6)
        self.assertEqual(again["effectiveLimits"], cfg["effectiveLimits"])
        self.assertFalse(Path(tmp, "gate.config.json.tmp").exists(), "临时文件已经换掉")


class TestRuntime(unittest.TestCase):
    def make(self, cfg: Dict[str, Any]) -> GateRuntime:
        rt = GateRuntime(logger=NullLogger())
        rt.start(cfg)
        self.addCleanup(rt.close)
        return rt

    def test_start_status_stop(self) -> None:
        up = FakeUpstream(self)
        rt = self.make(runtime_cfg(up.url))
        self.assertTrue(rt.running)
        status, _h, _d = http_request(rt.cfg["port"], body=b"{}")
        self.assertEqual(status, 200)
        snap = rt.status()
        self.assertTrue(snap["running"])
        self.assertEqual(snap["stats"]["admitted"], 1)
        self.assertIn("闸门未运行", status_lines({"running": False}))
        self.assertTrue(any("监听" in line for line in status_lines(snap)))

        rt.stop()
        self.assertFalse(rt.running)
        self.assertEqual(rt.status(), {"running": False})

    def test_apply_retry_count_live(self) -> None:
        """运行中把重试次数从 0 改到 2：同一个监听地址，下一次请求就扛住 429。"""
        up = LimitedUpstream(self, 99)
        cfg = runtime_cfg(up.url)
        cfg["retry"] = {**DEFAULT_RETRY, "maxRetries": 0, "cooldownMs": 20, "maxHoldMs": 5000}
        rt = self.make(cfg)
        port = rt.cfg["port"]

        status, _h, _d = http_request(port, body=b"{}")
        self.assertEqual(status, 429, "0 次重试：429 直接透传")
        self.assertEqual(up.hits, 1)

        # 上游只限流前 3 次，再把重试调到 2 次
        up._n = 3
        new = dict(rt.cfg)
        new["retry"] = {**rt.cfg["retry"], "maxRetries": 2}
        needs_restart, changes = rt.apply(new)
        self.assertFalse(needs_restart)
        self.assertEqual(changes, ["retry.maxRetries: 0 → 2"])
        self.assertEqual(rt.cfg["port"], port, "监听地址没变，客户端不用改 URL")

        status, _h, data = http_request(port, body=b"{}")
        self.assertEqual(status, 200, "改完即时生效：第 2、3 次被闸门扛下，第 4 次 200")
        self.assertEqual(json.loads(data)["attempt"], 4)

    def test_apply_upstream_live_on_keepalive(self) -> None:
        """改上游地址：同一条 keep-alive 连接上的下一个请求就走新上游。"""
        import http.client
        a, b = FakeUpstream(self), FakeUpstream(self)
        rt = self.make(runtime_cfg(a.url))
        conn = http.client.HTTPConnection("127.0.0.1", rt.cfg["port"], timeout=5)
        self.addCleanup(conn.close)

        conn.request("POST", "/v1/messages", body=b"{}", headers={"Content-Type": "application/json"})
        conn.getresponse().read()
        rt.apply(dict(rt.cfg, upstream=b.url))
        conn.request("POST", "/v1/messages", body=b"{}", headers={"Content-Type": "application/json"})
        conn.getresponse().read()

        self.assertEqual((len(a.state.requests), len(b.state.requests)), (1, 1), "第二个请求走到了新上游")

    def test_apply_limits_keeps_used_quota(self) -> None:
        """改限流参数时，已用掉的名额不清零，免得改个配置就放一波请求冲出去。"""
        up = FakeUpstream(self)
        cfg = runtime_cfg(up.url, rpm=5, windowMs=60000, limits=[],
                          effectiveLimits=[{"windowMs": 60000, "max": 5}])
        rt = self.make(cfg)
        for _ in range(3):
            http_request(rt.cfg["port"], body=b"{}")
        rt.apply(dict(rt.cfg, rpm=4, effectiveLimits=[{"windowMs": 60000, "max": 4}]))
        lim = rt.status()["limits"][0]
        self.assertEqual((lim["used"], lim["max"]), (3, 4))

    def test_apply_port_needs_restart(self) -> None:
        up = FakeUpstream(self)
        rt = self.make(runtime_cfg(up.url))
        port = rt.cfg["port"]
        needs_restart, changes = rt.apply(dict(rt.cfg, port=port + 1, rpm=99))
        self.assertTrue(needs_restart)
        self.assertIn(f"port: {port} → {port + 1}", changes)
        self.assertEqual(rt.cfg["port"], port, "重启前还显示真实在听的端口")
        self.assertEqual(rt.cfg["rpm"], 99, "其余改动照样即时生效")

        # 用 0 让系统分配，避免测试撞端口
        rt.restart(dict(rt.cfg, port=0))
        self.assertTrue(rt.running)
        self.assertNotEqual(rt.cfg["port"], 0)
        status, _h, _d = http_request(rt.cfg["port"], body=b"{}")
        self.assertEqual(status, 200)

    def test_apply_hook_path_reloads_hook(self) -> None:
        tmp = tempfile.mkdtemp(prefix="gate-rt-hook-")
        self.addCleanup(shutil.rmtree, tmp, True)
        hook_file = Path(tmp, "h.py")
        hook_file.write_text("def on_retry(ctx):\n    return {'maxRetries': 3}\n", encoding="utf-8")

        up = FakeUpstream(self)
        rt = self.make(runtime_cfg(up.url))
        self.assertFalse(rt.status()["retry"]["hook"].get("loaded", False))
        rt.apply(dict(rt.cfg, retry={**rt.cfg["retry"], "hook": str(hook_file)}))
        hook = rt.status()["retry"]["hook"]
        self.assertTrue(hook["enabled"])
        self.assertTrue(hook["loaded"])

        rt.apply(dict(rt.cfg, retry={**rt.cfg["retry"], "hook": ""}))
        self.assertTrue(rt.retry_hook is not None)

    def test_apply_while_stopped_is_used_on_start(self) -> None:
        up = FakeUpstream(self)
        rt = GateRuntime(logger=NullLogger())
        self.addCleanup(rt.close)
        self.assertEqual(rt.apply(runtime_cfg(up.url, rpm=42)), (False, []))
        rt.start(rt.cfg)
        self.assertEqual(rt.status()["limits"][-1]["max"], 42)

    def test_port_in_use_raises_system_exit(self) -> None:
        up = FakeUpstream(self)
        first = self.make(runtime_cfg(up.url))
        second = GateRuntime(logger=NullLogger())
        self.addCleanup(second.close)
        with self.assertRaises(SystemExit):
            second.start(runtime_cfg(up.url, port=first.cfg["port"]))
        self.assertFalse(second.running)

    def test_describe_changes(self) -> None:
        a = load_config(file=None, env={})
        b = dict(a, rpm=a["rpm"] + 1, retry={**a["retry"], "mode": "nextMinute"})
        self.assertEqual(describe_changes(a, b), [
            f"rpm: {a['rpm']} → {a['rpm'] + 1}",
            "retry.mode: cooldown → nextMinute",
        ])
        self.assertEqual(describe_changes(a, a), [])


class TestLoggerTap(unittest.TestCase):
    def test_tap_receives_lines_and_survives_errors(self) -> None:
        seen = []
        log = Logger("info", "")
        log.tap = lambda level, line: seen.append((level, line))
        log.info("你好", {"a": 1})
        self.assertEqual(seen[0][0], "info")
        self.assertIn("你好", seen[0][1])

        def broken(_level: str, _line: str) -> None:
            raise RuntimeError("window gone")
        log.tap = broken
        log.info("still fine")  # 不抛
        self.assertIsNone(log.tap, "回调坏了就摘掉，别每条日志都抛")

    def test_no_stdout_under_pythonw(self) -> None:
        """pythonw 下 sys.stdout 是 None，日志不能因此崩。"""
        import sys
        old = sys.stdout
        sys.stdout = None  # type: ignore[assignment]
        try:
            Logger("info", "").info("no console")
        finally:
            sys.stdout = old


if __name__ == "__main__":
    unittest.main()
