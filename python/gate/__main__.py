"""命令行入口：

    python -m gate            # 启动闸门
    python -m gate --ui       # 图形界面：改配置、起停、看实时状态
    python -m gate --status   # 查询正在跑的闸门状态
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


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0
    if args and args[0] == "--status":
        return print_status()
    if args and args[0] in ("--ui", "--gui"):
        from .ui import run  # 延迟导入：命令行启动不需要 tkinter
        return run(args[1:])
    if args:
        sys.stderr.write(f"[cc-rpm-gate] 不认识的参数: {' '.join(args)}\n{__doc__}")
        return 2
    return start()


if __name__ == "__main__":
    raise SystemExit(main())
