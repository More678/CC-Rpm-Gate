"""配置加载的单元测试（与 Node 版 test/config.test.js 一一对应）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from gate.config import (
    DEFAULTS,
    ROOT,
    ConfigError,
    effective_limits,
    load_config,
    normalize_limits,
    parse_duration,
)


def write_cfg(payload: dict) -> str:
    """写一个临时配置文件，返回路径（测试结束自行删除）。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="gate-cfg-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


class TestLoadConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._temp: list[str] = []

    def tearDown(self) -> None:
        for path in self._temp:
            try:
                os.unlink(path)
            except OSError:
                pass

    def cfg_file(self, payload: dict) -> str:
        path = write_cfg(payload)
        self._temp.append(path)
        return path

    def test_defaults(self) -> None:
        """默认配置：不读文件不读环境变量也能用。"""
        cfg = load_config(file=None, env={})
        self.assertEqual(cfg["port"], DEFAULTS["port"])
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["upstream"], "http://127.0.0.1:17521")
        self.assertEqual(cfg["maxConcurrent"], DEFAULTS["maxConcurrent"])
        self.assertEqual(cfg["bypassPaths"], ["/v1/messages/count_tokens"])
        # logFile 相对 cc-rpm-gate 目录解析成绝对路径
        self.assertEqual(Path(cfg["logFile"]).parent, ROOT / "logs")

    def test_file_overrides_defaults(self) -> None:
        """配置文件覆盖默认值，未写的字段保持默认。"""
        path = self.cfg_file({"port": 18000, "rpm": 7})
        cfg = load_config(file=path, env={})
        self.assertEqual(cfg["port"], 18000)
        self.assertEqual(cfg["rpm"], 7)
        self.assertEqual(cfg["windowMs"], DEFAULTS["windowMs"])

    def test_env_beats_file(self) -> None:
        """环境变量优先级高于配置文件。"""
        path = self.cfg_file({"port": 18000, "rpm": 7, "upstream": "http://127.0.0.1:1/"})
        cfg = load_config(
            file=path,
            env={"GATE_PORT": "19000", "GATE_RPM": "3", "GATE_UPSTREAM": "http://127.0.0.1:9999"},
        )
        self.assertEqual(cfg["port"], 19000)
        self.assertEqual(cfg["rpm"], 3)
        self.assertEqual(cfg["upstream"], "http://127.0.0.1:9999")

    def test_upstream_normalized(self) -> None:
        """upstream 归一化：去掉末尾斜杠，保留前缀路径。"""
        a = load_config(file=None, env={"GATE_UPSTREAM": "http://127.0.0.1:17521/"})
        self.assertEqual(a["upstream"], "http://127.0.0.1:17521")
        b = load_config(file=None, env={"GATE_UPSTREAM": "http://127.0.0.1:17521/api/v1/"})
        self.assertEqual(b["upstream"], "http://127.0.0.1:17521/api/v1")

    def test_invalid_values_raise(self) -> None:
        """非法配置：报错而不是悄悄用错值。"""
        cases = [
            ({"GATE_PORT": "0"}, "port"),
            ({"GATE_PORT": "70000"}, "port"),
            ({"GATE_RPM": "0"}, "rpm"),
            ({"GATE_MAX_CONCURRENT": "0"}, "maxConcurrent"),
            ({"GATE_MIN_GAP_MS": "-1"}, "minGapMs"),
            ({"GATE_MAX_QUEUE": "0"}, "maxQueue"),
            ({"GATE_QUEUE_TIMEOUT_MS": "0"}, "queueTimeoutMs"),
            ({"GATE_LOG_LEVEL": "loud"}, "logLevel"),
            ({"GATE_UPSTREAM": "ftp://127.0.0.1"}, "upstream"),
            ({"GATE_UPSTREAM": "不是URL"}, "upstream"),
            ({"GATE_RPM": "abc"}, "GATE_RPM"),
            ({"GATE_MAX_PENALTY_MS": "1"}, "maxPenaltyMs"),
        ]
        for env, needle in cases:
            with self.subTest(env=env):
                with self.assertRaises(ConfigError) as ctx:
                    load_config(file=None, env=env)
                self.assertIn(needle, str(ctx.exception))

    def test_bypass_paths_must_be_strings(self) -> None:
        path = self.cfg_file({"bypassPaths": "/a"})
        with self.assertRaises(ConfigError) as ctx:
            load_config(file=path, env={})
        self.assertIn("bypassPaths", str(ctx.exception))

    def test_unknown_fields_ignored(self) -> None:
        """未知字段被忽略，不会污染配置。"""
        path = self.cfg_file({"port": 18001, "prot": 19999, "随便写的": True})
        cfg = load_config(file=path, env={})
        self.assertEqual(cfg["port"], 18001)
        self.assertNotIn("prot", cfg)
        self.assertNotIn("随便写的", cfg)

    def test_empty_log_file(self) -> None:
        """logFile 为空时不解析成路径。"""
        cfg = load_config(file=None, env={}, overrides={"logFile": ""})
        self.assertEqual(cfg["logFile"], "")

    def test_json_float_ints_accepted(self) -> None:
        """JSON 里写成 20.0 的整数也能用（Python 里是 float）。"""
        path = self.cfg_file({"rpm": 20.0, "maxConcurrent": 3.0})
        cfg = load_config(file=path, env={})
        self.assertEqual(cfg["rpm"], 20)
        self.assertIsInstance(cfg["rpm"], int)
        self.assertEqual(cfg["maxConcurrent"], 3)

    def test_broken_json_raises(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="gate-cfg-")
        self._temp.append(path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("{ 不是合法 json")
        with self.assertRaises(ConfigError) as ctx:
            load_config(file=path, env={})
        self.assertIn("解析配置文件失败", str(ctx.exception))

    def test_missing_file_is_fine(self) -> None:
        cfg = load_config(file=str(ROOT / "不存在的配置.json"), env={})
        self.assertEqual(cfg["port"], DEFAULTS["port"])


class TestDurations(unittest.TestCase):
    def test_parse_duration(self) -> None:
        """parse_duration：裸数字按秒，支持 ms/s/m/h。"""
        self.assertEqual(parse_duration("10"), 10000)
        self.assertEqual(parse_duration("10s"), 10000)
        self.assertEqual(parse_duration("500ms"), 500)
        self.assertEqual(parse_duration("2m"), 120000)
        self.assertEqual(parse_duration("1h"), 3600000)
        self.assertEqual(parse_duration(" 30 S "), 30000)
        for bad in ("", "abc", "10x", "-5s", "0ms"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    parse_duration(bad)


class TestLimits(unittest.TestCase):
    def test_object_list(self) -> None:
        """每 X 秒 Y 次：对象数组写法。"""
        got = normalize_limits([{"seconds": 10, "max": 5}, {"windowMs": 1000, "max": 2}])
        self.assertEqual(got, [{"windowMs": 10000, "max": 5}, {"windowMs": 1000, "max": 2}])

    def test_text_shorthand(self) -> None:
        """每 X 秒 Y 次：字符串简写 "5/10s, 2/1s"。"""
        self.assertEqual(
            normalize_limits("5/10s, 2/1s"),
            [{"windowMs": 10000, "max": 5}, {"windowMs": 1000, "max": 2}],
        )
        # 裸数字按秒；单位大小写都认
        self.assertEqual(normalize_limits("3/30"), [{"windowMs": 30000, "max": 3}])
        self.assertEqual(normalize_limits("4/250MS"), [{"windowMs": 250, "max": 4}])

    def test_single_object_and_empty(self) -> None:
        """每 X 秒 Y 次：单个对象与空值。"""
        self.assertEqual(normalize_limits({"seconds": 5, "max": 1}), [{"windowMs": 5000, "max": 1}])
        for empty in (None, "", []):
            with self.subTest(empty=empty):
                self.assertEqual(normalize_limits(empty), [])

    def test_fractional_seconds(self) -> None:
        """每 X 秒 Y 次：小数秒转毫秒。"""
        self.assertEqual(normalize_limits([{"seconds": 0.5, "max": 1}]), [{"windowMs": 500, "max": 1}])

    def test_invalid_limits(self) -> None:
        """每 X 秒 Y 次：非法写法报错。"""
        cases = [
            ([{"max": 5}], "seconds 或 windowMs"),
            ([{"seconds": 10}], "max"),
            ([{"seconds": 10, "max": 0}], "max"),
            ([{"seconds": 10, "max": 1.5}], "max"),
            ([{"seconds": 0, "max": 1}], "seconds"),
            ([{"windowMs": 0, "max": 1}], "windowMs"),
            (["5/10s"], "必须是对象"),
            ("5", "limits 写法不认识"),
            ("abc/10s", "次数必须是整数"),
            (123, "必须是列表"),
        ]
        for value, needle in cases:
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as ctx:
                    normalize_limits(value)
                self.assertIn(needle, str(ctx.exception))

    def test_env_limits(self) -> None:
        """GATE_LIMITS 环境变量按字符串简写解析。"""
        cfg = load_config(file=None, env={"GATE_LIMITS": "5/10s, 2/1s"})
        self.assertEqual(cfg["limits"], [{"windowMs": 10000, "max": 5}, {"windowMs": 1000, "max": 2}])
        self.assertEqual(
            cfg["effectiveLimits"],
            [{"windowMs": 1000, "max": 2}, {"windowMs": 10000, "max": 5}, {"windowMs": 60000, "max": 20}],
        )

    def test_file_limits_text(self) -> None:
        """配置文件里用字符串写 limits 也认。"""
        fd, path = tempfile.mkstemp(suffix=".json", prefix="gate-cfg-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"limits": "2/5s"}, fh)
            cfg = load_config(file=path, env={})
            self.assertEqual(cfg["limits"], [{"windowMs": 5000, "max": 2}])
        finally:
            os.unlink(path)

    def test_effective_limits_sorted_and_strictest(self) -> None:
        """生效限流表：按窗口从短到长排序，同窗口取更严格的。"""
        got = effective_limits({
            "rpm": 20,
            "windowMs": 60000,
            "limits": [
                {"windowMs": 60000, "max": 9},   # 和主窗口撞车，取更小的 9
                {"windowMs": 1000, "max": 2},
                {"windowMs": 10000, "max": 5},
            ],
        })
        self.assertEqual(got, [
            {"windowMs": 1000, "max": 2},
            {"windowMs": 10000, "max": 5},
            {"windowMs": 60000, "max": 9},
        ])

    def test_effective_limits_includes_main_window(self) -> None:
        got = effective_limits({"rpm": 3, "windowMs": 300, "limits": []})
        self.assertEqual(got, [{"windowMs": 300, "max": 3}])


if __name__ == "__main__":
    unittest.main()
