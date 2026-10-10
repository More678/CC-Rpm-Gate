"""命令行入口：

    python -m gate            # 启动闸门
    python -m gate --ui       # 图形界面：改配置、起停、看实时状态
    python -m gate --status   # 查询正在跑的闸门状态
    python -m gate --init     # 在配置目录里写一份默认的 gate.config.json
    python -m gate --version

pip 从 git 安装后也可以用 cc-rpm-gate-py / cc-rpm-gate-ui 这两个命令。
配置目录：GATE_HOME → 代码目录（git clone）→ ~/.cc-rpm-gate（pip 安装）。
"""

from __future__ import annotations

import sys

from .server import print_status, start


def _force_utf8_stdout() -> None:
    """日志里有中文，控制台默认是 GBK 时会乱码甚至抛 UnicodeEncodeError。

    放在入口做，不走 bat 直接 `python -m gate` 也能正常输出。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass  # 被重定向成不支持重配的流，忽略


def init_config() -> int:
    """在配置目录里写一份默认配置。已经有了就不覆盖，只告诉你在哪。"""
    from .config import config_dir, load_config
    from .runtime import save_config

    target = config_dir() / "gate.config.json"
    if target.exists():
        sys.stdout.write(f"[cc-rpm-gate] 配置文件已经存在，没有改动: {target}\n")
        return 0
    path = save_config(load_config(file=None, env={}), target)
    sys.stdout.write(f"[cc-rpm-gate] 已写入默认配置: {path}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    if args and args[0] in ("-v", "--version"):
        from . import __version__
        sys.stdout.write(f"cc-rpm-gate {__version__} (python)\n")
        return 0
    if args and args[0] == "--status":
        return print_status()
    if args and args[0] == "--init":
        return init_config()
    if args and args[0] in ("--ui", "--gui"):
        from .ui import run  # 延迟导入：命令行启动不需要 tkinter
        return run(args[1:])
    if args:
        sys.stderr.write(f"[cc-rpm-gate] 不认识的参数: {' '.join(args)}\n{__doc__}")
        return 2
    return start()


if __name__ == "__main__":
    raise SystemExit(main())
