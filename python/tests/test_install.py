"""从 git 安装（pip install git+...#subdirectory=python）相关：

配置目录的查找规则、命令行入口、pyproject.toml 与 Node 版 package.json 的一致性。
与 Node 版 test/install.test.js 对应。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gate import __version__
from gate.config import HOME_DIR_NAME, ROOT, config_dir, load_config

PY_ROOT = Path(__file__).resolve().parents[1]


def run_cli(args, home):
    """跑 python -m gate，GATE_HOME 指到临时目录，避免碰到真实配置。"""
    env = dict(os.environ)
    env["GATE_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "gate", *args],
        cwd=str(PY_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )


class TestConfigDir(unittest.TestCase):
    def test_gate_home_wins(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(config_dir({"GATE_HOME": d}), Path(d).resolve())

    def test_code_dir_with_config_is_used(self) -> None:
        """git clone / release 压缩包：代码目录里有 gate.config.json 就用它。"""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "gate.config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(config_dir({}, d), Path(d))

    def test_installed_falls_back_to_home(self) -> None:
        """pip 装到 site-packages：包里不带 gate.config.json → ~/.cc-rpm-gate。"""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(config_dir({}, d), Path.home() / HOME_DIR_NAME)

    def test_repo_checkout_uses_repo_root(self) -> None:
        self.assertEqual(config_dir({}), ROOT, "本仓库里跑测试时配置目录就是仓库根目录")


class TestLoadConfigHome(unittest.TestCase):
    def test_paths_are_relative_to_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "gate.config.json").write_text(
                json.dumps({"rpm": 7, "logFile": "logs/x.log"}), encoding="utf-8")
            cfg = load_config(home=d, env={})
            self.assertEqual(cfg["rpm"], 7)
            self.assertEqual(cfg["configDir"], str(Path(d).resolve()))
            self.assertEqual(cfg["logFile"], str(Path(d).resolve() / "logs" / "x.log"))

    def test_env_arg_does_not_change_config_dir(self) -> None:
        cfg = load_config(env={"GATE_HOME": "/should/be/ignored", "GATE_RPM": "3"})
        self.assertEqual(cfg["rpm"], 3, "GATE_RPM 照样生效")
        self.assertEqual(cfg["configDir"], str(ROOT))

    def test_hook_resolves_against_config_dir(self) -> None:
        from gate.hooks import create_user_hook
        with tempfile.TemporaryDirectory() as d:
            Path(d, "gate.hooks.py").write_text(
                "def on_retry(ctx):\n    return {'maxRetries': 4}\n", encoding="utf-8")
            cfg = load_config(file=None, env={}, home=d)
            hook = create_user_hook(cfg)
            self.assertTrue(hook.enabled, "配置目录里的 gate.hooks.py 会被自动发现")
            self.assertEqual(hook.apply({"status": 429}), {"overrides": {"maxRetries": 4}})


class TestCli(unittest.TestCase):
    def test_version_and_help(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            v = run_cli(["--version"], d)
            self.assertEqual(v.returncode, 0, v.stderr)
            self.assertEqual(v.stdout.strip(), f"cc-rpm-gate {__version__} (python)")
            h = run_cli(["--help"], d)
            self.assertEqual(h.returncode, 0)
            self.assertIn("--init", h.stdout)
            bad = run_cli(["--nope"], d)
            self.assertEqual(bad.returncode, 2)

    def test_init_writes_then_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d, "nested", "home")  # 目录还不存在
            first = run_cli(["--init"], home)
            self.assertEqual(first.returncode, 0, first.stderr)
            target = home / "gate.config.json"
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["port"], 17530)
            self.assertEqual(written["retry"]["maxRetries"], 2)
            self.assertNotIn("configDir", written, "算出来的字段不写进配置文件")
            load_config(file=target, env={}, home=home)  # 写出来的能被正常加载

            target.write_text(json.dumps({"rpm": 9}), encoding="utf-8")
            second = run_cli(["--init"], home)
            self.assertEqual(second.returncode, 0)
            self.assertIn("已经存在", second.stdout)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["rpm"], 9)

    def test_status_exit_code_when_down(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "gate.config.json").write_text(json.dumps({"port": 1}), encoding="utf-8")
            r = run_cli(["--status"], d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("连不上闸门", r.stderr)


class TestPackaging(unittest.TestCase):
    def test_pyproject_matches_package_version(self) -> None:
        """pyproject.toml 的版本号取自 gate.__version__，和 package.json 必须一致。"""
        text = Path(PY_ROOT, "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = { attr = "gate.__version__" }', text)
        pkg = json.loads(Path(ROOT, "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["version"], __version__)

    def test_entry_points_are_importable(self) -> None:
        """pyproject.toml 里写的入口点必须真的存在，否则装完命令是坏的。"""
        import importlib
        for spec in ("gate.__main__:main", "gate.ui:run"):
            mod_name, attr = spec.split(":")
            mod = importlib.import_module(mod_name)
            self.assertTrue(callable(getattr(mod, attr)), spec)


if __name__ == "__main__":
    unittest.main()
