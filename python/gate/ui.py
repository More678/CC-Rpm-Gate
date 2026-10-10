"""闸门的图形界面：改配置、起停、看实时状态。

    python -m gate --ui        # 或双击 start-gate-ui.bat

用的是标准库 tkinter，零第三方依赖。闸门跑在同一个进程的后台线程里，
改完点「应用」即时生效：客户端（Claude Code / pi）的 ANTHROPIC_BASE_URL
不用动，监听地址没变，它们照常连 127.0.0.1:17530。
只有 host / port 改了才需要重启，界面会提示。

tkinter 是在用到的时候才 import 的：没装 tk 的环境（比如只跑测试的 CI）
也能 import 这个模块，拿到 FIELDS / form_to_config 这些纯函数。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RETRY, ConfigError, load_config
from .runtime import GateRuntime, load_or_error, save_config

# 界面上的字段表：(分组, 配置键, 标签, 控件, 说明)
# 配置键里带点的是 retry 的子项，例如 retry.maxRetries。
GROUP_BASIC = "基本"
GROUP_LIMIT = "限流"
GROUP_RETRY = "429 重试"
GROUP_LOG = "日志"

FIELDS: Tuple[Tuple[str, str, str, str, str], ...] = (
    (GROUP_BASIC, "host", "监听地址", "text", "只填 127.0.0.1，改成 0.0.0.0 会把你的上游额度暴露给局域网"),
    (GROUP_BASIC, "port", "监听端口", "int", "客户端要连的端口，改了需要重启闸门"),
    (GROUP_BASIC, "upstream", "默认上游", "text", "闸门把请求转给谁，默认是 CC-Switch 的本地代理"),
    (GROUP_BASIC, "upstreams", "命名上游", "json", 'pi 各 provider 用，形如 {"myprovider": "https://api.example.com"}'),

    (GROUP_LIMIT, "rpm", "每窗口请求数", "int", "主窗口内最多放行多少个请求"),
    (GROUP_LIMIT, "windowMs", "主窗口长度(ms)", "int", "60000 = 每分钟"),
    (GROUP_LIMIT, "limits", "额外限制", "text", '写法 "5/10s, 2/1s"：每 10 秒 5 次且每 1 秒 2 次，留空表示不加'),
    (GROUP_LIMIT, "maxConcurrent", "最大并发", "int", "同时在飞的请求数"),
    (GROUP_LIMIT, "minGapMs", "最小间隔(ms)", "int", "相邻两次放行至少隔多久"),
    (GROUP_LIMIT, "maxQueue", "队列上限", "int", "排队超过这个数就回 503"),
    (GROUP_LIMIT, "queueTimeoutMs", "排队超时(ms)", "int", "排这么久还没轮到就回 504"),
    (GROUP_LIMIT, "penaltyMs", "冷却基数(ms)", "int", "上游限流后全局冷却多久（连续限流会指数退避）"),
    (GROUP_LIMIT, "maxPenaltyMs", "冷却上限(ms)", "int", "退避到此为止"),
    (GROUP_LIMIT, "bypassPaths", "不排队路径", "list", "逗号分隔，这些路径直接转发不排队"),

    (GROUP_RETRY, "retry.enabled", "开启重试", "bool", "关掉后 429 直接透传给客户端"),
    (GROUP_RETRY, "retry.maxRetries", "重试次数", "int", "闸门自己重试几次，0 = 不重试"),
    (GROUP_RETRY, "retry.mode", "暂存模式", "choice:cooldown,nextMinute", "cooldown = 冷却若干秒；nextMinute = 等到下一分钟"),
    (GROUP_RETRY, "retry.cooldownMs", "冷却时长(ms)", "int", "cooldown 模式等多久"),
    (GROUP_RETRY, "retry.nextMinuteOffsetMs", "分钟偏移(ms)", "int", "1000 = 下一分钟第 01 秒，0 = 第 00 秒"),
    (GROUP_RETRY, "retry.respectRetryAfter", "听上游 Retry-After", "bool", "上游要求等更久时听它的"),
    (GROUP_RETRY, "retry.maxHoldMs", "单请求暂存预算(ms)", "int", "一个请求最多被暂存多久，超了透传 429"),
    (GROUP_RETRY, "retry.maxStashed", "同时暂存上限", "int", "暂存满了就透传 429"),
    (GROUP_RETRY, "retry.statuses", "限流状态码", "list", "逗号分隔，默认 429,529"),
    (GROUP_RETRY, "retry.hook", "钩子文件", "text", "留空=自动找 gate.hooks.py；见 gate.hooks.example.py"),

    (GROUP_LOG, "logFile", "日志文件", "text", "相对仓库根目录，留空表示只打控制台"),
    (GROUP_LOG, "logLevel", "日志级别", "choice:debug,info,warn,error,silent", ""),
)

GROUPS = (GROUP_BASIC, GROUP_LIMIT, GROUP_RETRY, GROUP_LOG)


def _get(cfg: Dict[str, Any], key: str) -> Any:
    if "." in key:
        head, tail = key.split(".", 1)
        return (cfg.get(head) or {}).get(tail)
    return cfg.get(key)


def config_to_form(cfg: Dict[str, Any]) -> Dict[str, str]:
    """配置 → 界面上的字符串值。"""
    form: Dict[str, str] = {}
    for _group, key, _label, kind, _hint in FIELDS:
        val = _get(cfg, key)
        if kind == "bool":
            form[key] = "1" if val else "0"
        elif kind == "json":
            form[key] = json.dumps(val or {}, ensure_ascii=False)
        elif kind == "list":
            form[key] = ", ".join(str(v) for v in (val or []))
        elif key == "limits":
            # 存的是 [{windowMs, max}]，界面上用紧凑写法 "5/10s"
            form[key] = ", ".join(_limit_text(l) for l in (val or []))
        elif key == "logFile":
            form[key] = _rel_log(val, cfg.get("configDir"))
        else:
            form[key] = "" if val is None else str(val)
    return form


def _limit_text(lim: Dict[str, Any]) -> str:
    ms = int(lim["windowMs"])
    if ms % 60000 == 0:
        return f"{lim['max']}/{ms // 60000}m"
    if ms % 1000 == 0:
        return f"{lim['max']}/{ms // 1000}s"
    return f"{lim['max']}/{ms}ms"


def _rel_log(val: Any, base: Any = None) -> str:
    """日志路径显示成相对配置目录的写法，和 gate.config.json 里的写法一致。"""
    if not val:
        return ""
    from pathlib import Path
    from .config import config_dir
    try:
        return Path(val).relative_to(base or config_dir()).as_posix()
    except ValueError:
        return str(val)


def form_to_config(form: Dict[str, str]) -> Dict[str, Any]:
    """界面上的字符串值 → 校验过的完整配置。

    校验、归一化全都复用 load_config，所以界面和命令行的规则完全一致。
    不读配置文件也不读环境变量：界面上显示的就是最终生效的值。
    """
    overrides: Dict[str, Any] = {}
    retry: Dict[str, Any] = {}

    for _group, key, label, kind, _hint in FIELDS:
        raw = (form.get(key) or "").strip()
        target, name = (retry, key.split(".", 1)[1]) if "." in key else (overrides, key)

        if kind == "bool":
            target[name] = raw in ("1", "true", "True", "yes", "on")
        elif kind == "int":
            if raw == "":
                raise ConfigError(f"{label} 不能留空")
            try:
                target[name] = int(raw)
            except ValueError:
                raise ConfigError(f"{label} 必须是整数，当前值: {raw}") from None
        elif kind == "json":
            try:
                target[name] = json.loads(raw) if raw else {}
            except json.JSONDecodeError as err:
                raise ConfigError(f"{label} 不是合法 JSON: {err}") from None
        elif kind == "list":
            target[name] = [p.strip() for p in raw.split(",") if p.strip()]
        elif kind.startswith("choice:"):
            target[name] = raw
        else:
            target[name] = raw

    # retry 的默认值兜底：界面上没出现的字段（比如以后新加的）保持默认
    overrides["retry"] = {**DEFAULT_RETRY, **retry}
    # limits 的字符串写法交给 normalize_limits 解析（"5/10s, 2/1s"）
    return load_config(file=None, env={}, overrides=overrides)


def status_lines(snap: Dict[str, Any]) -> List[str]:
    """把状态快照说成几行人话，显示在界面下半部分。"""
    if not snap.get("running"):
        return ["闸门未运行"]
    retry = snap.get("retry") or {}
    stats = retry.get("stats") or {}
    hook = retry.get("hook") or {}
    lines = [
        f"监听 {snap.get('listen', '')}  →  上游 {snap.get('upstream', '')}",
        f"排队 {snap.get('queued', 0)}（其中暂存重试 {snap.get('queuedPriority', 0)}）　"
        f"在飞 {snap.get('inFlight', 0)}/{snap.get('maxConcurrent', 0)}　"
        f"冷却剩余 {snap.get('penaltyMsLeft', 0)}ms",
        f"窗口用量 {_usage_text(snap.get('limits') or [])}",
        f"放行 {(snap.get('stats') or {}).get('admitted', 0)}　"
        f"拒绝 {(snap.get('stats') or {}).get('rejected', 0)}　"
        f"超时 {(snap.get('stats') or {}).get('timedOut', 0)}　"
        f"冷却次数 {(snap.get('stats') or {}).get('penalties', 0)}",
        f"暂存中 {retry.get('stashed', 0)}　下一个放行 {retry.get('nextReleaseInMs', 0)}ms　"
        f"重试 {stats.get('retried', 0)}　救回 {stats.get('recovered', 0)}　"
        f"用尽 {stats.get('exhausted', 0)}　透传 {stats.get('passedThrough', 0)}",
    ]
    if hook.get("enabled"):
        state = "已加载" if hook.get("loaded") else "未加载"
        hstats = hook.get("stats") or {}
        line = (f"钩子 {hook.get('file')}（{state}）　调用 {hstats.get('calls', 0)}　"
                f"覆盖 {hstats.get('overrides', 0)}　拦下 {hstats.get('forcedPassthrough', 0)}　"
                f"出错 {hstats.get('errors', 0)}")
        if hook.get("lastError"):
            line += f"\n钩子最近报错：{hook['lastError']}"
        lines.append(line)
    return lines


def _usage_text(limits: List[Dict[str, Any]]) -> str:
    if not limits:
        return "（无）"
    parts = []
    for l in limits:
        text = f"{l.get('used', 0)}/{l.get('max', 0)} 每 {_limit_text(l).split('/', 1)[1]}"
        if l.get("resetInMs"):
            text += f"（{l['resetInMs']}ms 后有名额）"
        parts.append(text)
    return "，".join(parts)


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

REFRESH_MS = 1000      # 状态区刷新间隔
LOG_MAX_LINES = 500    # 日志区最多留多少行，免得开一天把内存吃满


class GateWindow:
    """主窗口。所有 tk 调用都在主线程；闸门的日志从后台线程经队列转进来。"""

    def __init__(self, root: Any, runtime: GateRuntime) -> None:
        import queue
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.runtime = runtime
        self.vars: Dict[str, Any] = {}
        self._log_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._dirty = False   # 表单改过但还没应用

        root.title("cc-rpm-gate")
        root.minsize(760, 620)
        self._build_form()
        self._build_buttons()
        self._build_status()
        self._build_log()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(REFRESH_MS, self._tick)

    # ---------- 布局 ----------

    def _build_form(self) -> None:
        tk, ttk = self.tk, self.ttk
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="x", padx=8, pady=(8, 4))
        frames = {}
        for group in GROUPS:
            frame = ttk.Frame(notebook, padding=8)
            frame.columnconfigure(1, weight=1)
            notebook.add(frame, text=group)
            frames[group] = frame

        rows: Dict[str, int] = {g: 0 for g in GROUPS}
        for group, key, label, kind, hint in FIELDS:
            frame, row = frames[group], rows[group]
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar()
            if kind == "bool":
                widget = ttk.Checkbutton(frame, variable=var, onvalue="1", offvalue="0")
            elif kind.startswith("choice:"):
                widget = ttk.Combobox(frame, textvariable=var, state="readonly",
                                      values=kind.split(":", 1)[1].split(","))
            else:
                widget = ttk.Entry(frame, textvariable=var)
            widget.grid(row=row, column=1, sticky="ew", pady=2)
            if hint:
                ttk.Label(frame, text=hint, foreground="#666").grid(
                    row=row, column=2, sticky="w", padx=(8, 0))
            var.trace_add("write", lambda *_a: self._mark_dirty())
            self.vars[key] = var
            rows[group] = row + 1

    def _build_buttons(self) -> None:
        ttk = self.ttk
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="启动闸门", command=self.on_start_stop)
        self.btn_start.pack(side="left")
        ttk.Button(bar, text="应用（即时生效）", command=self.on_apply).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="保存到 gate.config.json", command=self.on_save).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="从文件重新载入", command=self.on_reload).pack(side="left", padx=(8, 0))
        self.hint = ttk.Label(bar, text="", foreground="#a60")
        self.hint.pack(side="left", padx=(12, 0))

    def _build_status(self) -> None:
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(self.root, text="实时状态", padding=8)
        box.pack(fill="x", padx=8, pady=4)
        self.status_text = tk.StringVar(value="闸门未运行")
        ttk.Label(box, textvariable=self.status_text, justify="left", font=("Consolas", 10)).pack(anchor="w")

    def _build_log(self) -> None:
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(self.root, text="日志", padding=4)
        box.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log = tk.Text(box, height=10, wrap="none", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---------- 表单 ----------

    def load_form(self, cfg: Dict[str, Any]) -> None:
        for key, text in config_to_form(cfg).items():
            self.vars[key].set(text)
        self._dirty = False
        self.hint.configure(text="")

    def read_form(self) -> Dict[str, str]:
        return {key: var.get() for key, var in self.vars.items()}

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            if self.runtime.running:
                self.hint.configure(text="改动还没生效，点「应用」")

    # ---------- 按钮 ----------

    def on_start_stop(self) -> None:
        from tkinter import messagebox
        if self.runtime.running:
            self.runtime.stop()
            self._note("闸门已停止")
        else:
            try:
                cfg = form_to_config(self.read_form())
            except ConfigError as err:
                messagebox.showerror("配置有问题", str(err))
                return
            try:
                self.runtime.start(cfg)
            except SystemExit:
                # build() 在端口占用 / 没权限时会 SystemExit(1)，日志里已经说了原因
                messagebox.showerror(
                    "启动失败",
                    f"端口 {cfg['host']}:{cfg['port']} 起不来。"
                    "可能已经有一个闸门在跑（命令行版或另一个窗口），或者端口被别的程序占了。")
                return
            if self.runtime.logger is not None:
                self.runtime.logger.tap = self._on_log
            self._note(f"闸门已启动：{self.runtime.listen_url()}")
            self._dirty = False
            self.hint.configure(text="")
        self._refresh()

    def on_apply(self) -> None:
        from tkinter import messagebox
        try:
            cfg = form_to_config(self.read_form())
        except ConfigError as err:
            messagebox.showerror("配置有问题", str(err))
            return
        if not self.runtime.running:
            self.runtime.apply(cfg)
            self._note("闸门没在跑，配置已记下，启动时生效")
            self._dirty = False
            self.hint.configure(text="")
            return

        needs_restart, changes = self.runtime.apply(cfg)
        if not changes:
            self._note("配置没有变化")
            self._dirty = False
            self.hint.configure(text="")
            return
        # host / port 这两项要重启才生效，别和其它改动一起说成「已生效」
        pending = [c for c in changes if c.split(":", 1)[0] in ("host", "port")]
        applied = [c for c in changes if c not in pending]
        if applied:
            self._note("已生效：" + "；".join(applied))
        if pending:
            self._note("待重启后生效：" + "；".join(pending))
        self._dirty = False
        if needs_restart:
            self.hint.configure(text="监听地址改了，需要重启闸门")
            if messagebox.askyesno(
                    "需要重启",
                    "改了监听地址或端口，必须重启闸门才能生效。\n\n"
                    "重启期间排队中的请求会收到 503，而且客户端那边的 "
                    "ANTHROPIC_BASE_URL 也得跟着改成新端口。\n\n现在重启吗？"):
                try:
                    self.runtime.restart(cfg)
                except SystemExit:
                    messagebox.showerror("重启失败", f"新端口 {cfg['host']}:{cfg['port']} 起不来，闸门现在是停止状态。")
                else:
                    if self.runtime.logger is not None:
                        self.runtime.logger.tap = self._on_log
                    self._note(f"已重启：{self.runtime.listen_url()}")
                    self.hint.configure(text="")
        else:
            self.hint.configure(text="")
        self._refresh()

    def on_save(self) -> None:
        from tkinter import messagebox
        try:
            cfg = form_to_config(self.read_form())
        except ConfigError as err:
            messagebox.showerror("配置有问题", str(err))
            return
        try:
            path = save_config(cfg)
        except OSError as err:
            messagebox.showerror("保存失败", str(err))
            return
        self._note(f"已保存到 {path}（Node 版下次启动也用这份）")

    def on_reload(self) -> None:
        from tkinter import messagebox
        cfg, err = load_or_error()
        if cfg is None:
            messagebox.showerror("配置有问题", err)
            return
        self.load_form(cfg)
        self._note("已从 gate.config.json 重新载入（还没应用到运行中的闸门）")

    def on_close(self) -> None:
        from tkinter import messagebox
        if self.runtime.running and not messagebox.askyesno(
                "闸门还在跑", "关掉窗口会一起停掉闸门，客户端的请求会失败。确定要关吗？"):
            return
        if self.runtime.logger is not None:
            self.runtime.logger.tap = None
        self.runtime.close()
        self.root.destroy()

    # ---------- 刷新 ----------

    def _on_log(self, _level: str, line: str) -> None:
        """闸门线程调这个，只往队列里塞，真正写界面在主线程的 _tick 里。"""
        self._log_queue.put((_level, line))

    def _tick(self) -> None:
        self._drain_log()
        self._refresh()
        self.root.after(REFRESH_MS, self._tick)

    def _drain_log(self) -> None:
        import queue
        lines = []
        while True:
            try:
                lines.append(self._log_queue.get_nowait()[1])
            except queue.Empty:
                break
        if not lines:
            return
        self.log.configure(state="normal")
        self.log.insert("end", "".join(lines))
        # 超长就从头删，保持在 LOG_MAX_LINES 行
        total = int(self.log.index("end-1c").split(".")[0])
        if total > LOG_MAX_LINES:
            self.log.delete("1.0", f"{total - LOG_MAX_LINES}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def _refresh(self) -> None:
        running = self.runtime.running
        self.btn_start.configure(text="停止闸门" if running else "启动闸门")
        self.status_text.set("\n".join(status_lines(self.runtime.status())))

    def _note(self, message: str) -> None:
        self._log_queue.put(("info", message + "\n"))
        self._drain_log()


def run(argv: Optional[List[str]] = None) -> int:
    """打开窗口。配置读不出来也照样开窗，让用户在界面上改。"""
    try:
        import tkinter as tk
    except ImportError:
        import sys
        sys.stderr.write(
            "[cc-rpm-gate] 这个 Python 没装 tkinter，开不了图形界面。\n"
            "  Windows: 用 python.org 的安装包，勾上 tcl/tk\n"
            "  Linux:   先装 python3-tk\n"
            "  或者直接用命令行版：python -m gate\n")
        return 1

    cfg, err = load_or_error()
    root = tk.Tk()
    runtime = GateRuntime()
    win = GateWindow(root, runtime)
    if cfg is None:
        from tkinter import messagebox
        win.load_form(load_config(file=None, env={}))
        messagebox.showwarning("配置有问题", f"{err}\n\n界面里先用的是默认值，改好后点保存。")
    else:
        win.load_form(cfg)
        # 开窗就把闸门拉起来，省一次点击；起不来（端口被占）就只报一句，窗口照开
        try:
            runtime.start(cfg)
        except SystemExit:
            win._note(f"端口 {cfg['host']}:{cfg['port']} 起不来，可能已经有闸门在跑了")
        else:
            if runtime.logger is not None:
                runtime.logger.tap = win._on_log
            win._note(f"闸门已启动：{runtime.listen_url()}")
    win._refresh()
    root.mainloop()
    return 0
