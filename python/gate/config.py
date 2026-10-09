"""配置加载：默认值 → gate.config.json → 环境变量，后者覆盖前者。

与 Node 版共用同一个 gate.config.json 和同一套环境变量名，
两个版本任选其一启动即可，配置只需要写一份。
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

# <repo>/python/gate/config.py → <repo>
ROOT = Path(__file__).resolve().parents[2]

# 429 重试钩子的默认值。闸门自己扛下限流重试，客户端（Claude Code / pi）看不到 429。
DEFAULT_RETRY: Dict[str, Any] = {
    "enabled": True,
    "maxRetries": 2,              # 闸门自己重试几次；0 = 不重试，直接透传 429
    "statuses": [429, 529],       # 哪些状态码算限流
    "mode": "cooldown",           # cooldown | nextMinute
    "cooldownMs": 15000,          # cooldown 模式：固定等多久
    "nextMinuteOffsetMs": 1000,   # nextMinute 模式：对齐到下一分钟的第几毫秒（1000 = 第 01 秒）
    "respectRetryAfter": True,    # 上游 Retry-After 比冷却更久时听上游的
    "maxHoldMs": 180000,          # 单个请求累计最多被暂存多久，超了就透传 429
    "maxStashed": 50,             # 同时暂存的请求数上限，超了就透传 429
    # 用户钩子文件：留空时看默认的 gate.hooks.py / gate.hooks.js 在不在，
    # 在就加载。钩子能按请求改这次的重试次数和等待方式，见 gate/hooks.py
    "hook": "",
}

RETRY_INT_KEYS = ("maxRetries", "cooldownMs", "nextMinuteOffsetMs", "maxHoldMs", "maxStashed")
RETRY_BOOL_KEYS = ("enabled", "respectRetryAfter")
RETRY_MODES = ("cooldown", "nextMinute")

# retry.* 的环境变量覆盖
RETRY_ENV_MAP = {
    "GATE_RETRY_ENABLED": "enabled",
    "GATE_RETRY_MAX": "maxRetries",
    "GATE_MAX_RETRIES": "maxRetries",       # 别名，好记
    "GATE_RETRY_STATUSES": "statuses",
    "GATE_RETRY_MODE": "mode",
    "GATE_RETRY_COOLDOWN_MS": "cooldownMs",
    "GATE_RETRY_COOLDOWN_SECONDS": "cooldownSeconds",
    "GATE_RETRY_NEXT_MINUTE_OFFSET_MS": "nextMinuteOffsetMs",
    "GATE_RETRY_RESPECT_RETRY_AFTER": "respectRetryAfter",
    "GATE_RETRY_MAX_HOLD_MS": "maxHoldMs",
    "GATE_RETRY_MAX_STASHED": "maxStashed",
    "GATE_RETRY_HOOK": "hook",
}

DEFAULTS: Dict[str, Any] = {
    "port": 17530,                        # 闸门自身监听端口
    "host": "127.0.0.1",                  # 只监听本地，不对外暴露
    "upstream": "http://127.0.0.1:17521",  # 上游（CC-Switch 本地代理）
    "rpm": 20,                            # 主窗口：windowMs 内最多放行多少个请求
    "windowMs": 60000,                    # 主窗口长度
    # 额外的「每 X 秒最多 Y 次」限制，可写多条，必须同时满足才放行
    "limits": [{"seconds": 10, "max": 5}],
    "maxConcurrent": 2,                   # 同时在飞的请求数上限
    "minGapMs": 300,                      # 相邻两次放行的最小间隔
    "maxQueue": 200,                      # 队列长度上限，超过直接 503
    "queueTimeoutMs": 600000,             # 单个请求最长排队时间
    "penaltyMs": 15000,                   # 上游 429/529 后的全局冷却基数
    "maxPenaltyMs": 120000,               # 冷却时间上限（连续 429 会指数退避）
    "bypassPaths": ["/v1/messages/count_tokens"],  # 不排队直接转发的路径
    # 429 重试钩子，见 DEFAULT_RETRY
    "retry": DEFAULT_RETRY,
    # 额外的命名上游：{"名字": "https://..."}，客户端用 /__gate/u/<名字>/... 访问。
    # pi 的每个 provider 各有自己的 baseUrl，靠这个共用一套限流。
    "upstreams": {},
    "logFile": "logs/gate-py.log",
    "logLevel": "info",                   # debug | info | warn | error | silent
}

INT_KEYS = (
    "port", "rpm", "windowMs", "maxConcurrent", "minGapMs",
    "maxQueue", "queueTimeoutMs", "penaltyMs", "maxPenaltyMs",
)

ENV_MAP = {
    "GATE_PORT": "port",
    "GATE_HOST": "host",
    "GATE_UPSTREAM": "upstream",
    "GATE_UPSTREAMS": "upstreams",   # JSON 字符串，例如 {"myprovider":"https://..."}
    "GATE_RPM": "rpm",
    "GATE_WINDOW_MS": "windowMs",
    "GATE_LIMITS": "limits",
    "GATE_MAX_CONCURRENT": "maxConcurrent",
    "GATE_MIN_GAP_MS": "minGapMs",
    "GATE_MAX_QUEUE": "maxQueue",
    "GATE_QUEUE_TIMEOUT_MS": "queueTimeoutMs",
    "GATE_PENALTY_MS": "penaltyMs",
    "GATE_MAX_PENALTY_MS": "maxPenaltyMs",
    "GATE_LOG_FILE": "logFile",
    "GATE_LOG_LEVEL": "logLevel",
}

LOG_LEVELS = ("debug", "info", "warn", "error", "silent")

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000}


class ConfigError(ValueError):
    """配置不合法。"""


def _coerce_int(value: Any) -> Any:
    """JSON 里的 20.0 在 Python 里是 float，这里统一收成 int（和 Node 版行为对齐）。

    不是整数值就原样返回，交给调用方报错。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_duration(text: Any) -> int:
    """把 "10s" / "500ms" / "2m" / "10" 解析成毫秒；裸数字按秒算。"""
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ConfigError(f"时间窗写法不认识: {text}（示例 10s / 500ms / 2m）")
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    ms = n * _UNIT_MS[unit]
    if ms < 1:
        raise ConfigError(f"时间窗必须 ≥ 1ms: {text}")
    return ms


def _parse_limits_text(value: str) -> List[Dict[str, int]]:
    """解析 "5/10s, 2/1s" 这种简写（次数/时间窗）。"""
    entries: List[Dict[str, int]] = []
    for part in (p.strip() for p in value.split(",")):
        if not part:
            continue
        if "/" not in part:
            raise ConfigError(f"limits 写法不认识: {part}（示例 5/10s 表示每 10 秒 5 次）")
        max_text, _, window_text = part.partition("/")
        try:
            max_n = int(str(max_text).strip())
        except ValueError:
            raise ConfigError(f"limits 次数必须是整数: {part}") from None
        entries.append({"max": max_n, "windowMs": parse_duration(window_text)})
    return entries


def normalize_limits(value: Any) -> List[Dict[str, int]]:
    """归一化「每 X 秒 Y 次」配置。

    支持三种写法：
      - 列表：[{"seconds": 10, "max": 5}, {"windowMs": 1000, "max": 2}]
      - 字符串："5/10s, 2/1s"
      - 单个字典：{"seconds": 10, "max": 5}
    """
    if value is None or value == "" or value == []:
        return []

    if isinstance(value, str):
        entries: Iterable[Any] = _parse_limits_text(value)
    elif isinstance(value, dict):
        entries = [value]
    elif isinstance(value, (list, tuple)):
        entries = value
    else:
        raise ConfigError(f"limits 必须是列表、字典或字符串，当前类型: {type(value).__name__}")

    out: List[Dict[str, int]] = []
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ConfigError(f'limits[{i}] 必须是对象，例如 {{"seconds": 10, "max": 5}}')
        raw_max = item.get("max")
        if not isinstance(raw_max, int) or isinstance(raw_max, bool) or raw_max < 1:
            raise ConfigError(f"limits[{i}].max 必须是 ≥ 1 的整数，当前值: {raw_max!r}")

        if item.get("windowMs") is not None:
            window_ms = item["windowMs"]
            if not isinstance(window_ms, int) or isinstance(window_ms, bool) or window_ms < 1:
                raise ConfigError(f"limits[{i}].windowMs 必须是 ≥ 1 的整数，当前值: {window_ms!r}")
        elif item.get("seconds") is not None:
            seconds = item["seconds"]
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ConfigError(f"limits[{i}].seconds 必须 > 0，当前值: {seconds!r}")
            window_ms = round(seconds * 1000)
        else:
            raise ConfigError(f"limits[{i}] 需要 seconds 或 windowMs 字段")

        out.append({"windowMs": int(window_ms), "max": int(raw_max)})
    return out


def effective_limits(cfg: Dict[str, Any]) -> List[Dict[str, int]]:
    """主窗口（rpm/windowMs）+ 额外 limits 合并成一张生效的限流表，去重。"""
    merged: Dict[int, int] = {}
    for lim in [{"windowMs": cfg["windowMs"], "max": cfg["rpm"]}, *cfg["limits"]]:
        window_ms, max_n = lim["windowMs"], lim["max"]
        prev = merged.get(window_ms)
        # 同一个窗口写了多次，取更严格的那个
        if prev is None or max_n < prev:
            merged[window_ms] = max_n
    return [{"windowMs": w, "max": m} for w, m in sorted(merged.items())]


def parse_bool(raw: Any, label: str) -> bool:
    """环境变量里的布尔值：1/true/yes/on 为真，0/false/no/off 为假。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{label} 必须是 true/false，当前值: {raw}")


def normalize_retry(value: Any) -> Dict[str, Any]:
    """归一化 retry 配置。

    statuses 支持列表或 "429,529" 字符串；cooldownSeconds / cooldown 是 cooldownMs 的糖。
    """
    if value is None:
        return dict(DEFAULT_RETRY)
    if isinstance(value, bool):
        return {**DEFAULT_RETRY, "enabled": value}
    if not isinstance(value, dict):
        raise ConfigError(f"retry 必须是对象或布尔值，当前类型: {type(value).__name__}")

    out: Dict[str, Any] = {**DEFAULT_RETRY, **value}

    # cooldown / cooldownSeconds → cooldownMs
    if value.get("cooldownMs") is None:
        if value.get("cooldownSeconds") is not None:
            sec = value["cooldownSeconds"]
            if isinstance(sec, bool) or not isinstance(sec, (int, float)) or sec < 0:
                raise ConfigError(f"retry.cooldownSeconds 必须 ≥ 0，当前值: {sec!r}")
            out["cooldownMs"] = round(sec * 1000)
        elif value.get("cooldown") is not None:
            out["cooldownMs"] = parse_duration(value["cooldown"])
    out.pop("cooldownSeconds", None)
    out.pop("cooldown", None)

    for key in RETRY_BOOL_KEYS:
        if not isinstance(out[key], bool):
            raise ConfigError(f"retry.{key} 必须是布尔值，当前值: {out[key]!r}")
    for key in RETRY_INT_KEYS:
        n = _coerce_int(out[key])
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ConfigError(f"retry.{key} 必须是 ≥ 0 的整数，当前值: {out[key]!r}")
        out[key] = n
    if out["mode"] not in RETRY_MODES:
        raise ConfigError(f"retry.mode 必须是 {'/'.join(RETRY_MODES)} 之一，当前值: {out['mode']!r}")
    if out["maxStashed"] < 1:
        raise ConfigError(f"retry.maxStashed 必须 ≥ 1，当前值: {out['maxStashed']}")
    if out["nextMinuteOffsetMs"] >= 60000:
        raise ConfigError(f"retry.nextMinuteOffsetMs 必须小于 60000，当前值: {out['nextMinuteOffsetMs']}")

    statuses = out["statuses"]
    if isinstance(statuses, str):
        statuses = [s.strip() for s in statuses.split(",") if s.strip()]
    if not isinstance(statuses, (list, tuple)) or len(statuses) == 0:
        raise ConfigError("retry.statuses 必须是非空的状态码列表，例如 [429, 529]")
    codes = []
    for s in statuses:
        n = _coerce_int(s)
        if isinstance(n, str):
            try:
                n = int(n.strip())
            except ValueError:
                raise ConfigError(f"retry.statuses 里的状态码不合法: {s!r}") from None
        if not isinstance(n, int) or isinstance(n, bool) or not 100 <= n <= 599:
            raise ConfigError(f"retry.statuses 里的状态码不合法: {s!r}")
        codes.append(n)
    out["statuses"] = codes

    # hook：False / None 当成不启用；其余必须是路径字符串
    if out["hook"] is False or out["hook"] is None:
        out["hook"] = ""
    if not isinstance(out["hook"], str):
        raise ConfigError(
            f"retry.hook 必须是钩子文件路径字符串，当前类型: {type(out['hook']).__name__}")
    out["hook"] = out["hook"].strip()

    # 只保留已知字段，拼错的名字不要静默生效
    return {key: out[key] for key in DEFAULT_RETRY}


def normalize_upstreams(value: Any) -> Dict[str, str]:
    """归一化命名上游表：值走 _normalize_upstream 同一套校验。"""
    if value is None or value == "" or value == {}:
        return {}
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ConfigError(f"upstreams 不是合法 JSON: {err}") from None
    if not isinstance(raw, dict):
        raise ConfigError('upstreams 必须是 {"名字": "http://..."} 形式的对象')
    out: Dict[str, str] = {}
    for name, target in raw.items():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(name)):
            raise ConfigError(f"upstreams 的名字只能用字母数字和 . _ -，当前值: {name}")
        out[str(name)] = _normalize_upstream(str(target))
    return out


def _read_config_file(file: Path) -> Dict[str, Any]:
    if not file.exists():
        return {}
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"读取配置文件失败 {file}: {err}") from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ConfigError(f"解析配置文件失败 {file}: {err}") from None
    if not isinstance(parsed, dict):
        raise ConfigError(f"解析配置文件失败 {file}: 顶层必须是 JSON 对象")
    return parsed


def _normalize_upstream(value: Any) -> str:
    from urllib.parse import urlsplit

    text = str(value)
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"upstream 只支持 http/https: {text}")
    if not parts.hostname:
        raise ConfigError(f"upstream 不是合法 URL: {text}")
    # 去掉末尾斜杠，转发时直接拼接原始 path
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _validate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for key in INT_KEYS:
        v = cfg[key]
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(f"配置 {key} 必须是整数，当前值: {v!r}")
    if not 1 <= cfg["port"] <= 65535:
        raise ConfigError(f"配置 port 超出范围: {cfg['port']}")
    if cfg["rpm"] < 1:
        raise ConfigError(f"配置 rpm 必须 ≥ 1，当前值: {cfg['rpm']}")
    if cfg["windowMs"] < 1:
        raise ConfigError(f"配置 windowMs 必须 ≥ 1，当前值: {cfg['windowMs']}")
    if cfg["maxConcurrent"] < 1:
        raise ConfigError(f"配置 maxConcurrent 必须 ≥ 1，当前值: {cfg['maxConcurrent']}")
    if cfg["minGapMs"] < 0:
        raise ConfigError(f"配置 minGapMs 不能为负: {cfg['minGapMs']}")
    if cfg["maxQueue"] < 1:
        raise ConfigError(f"配置 maxQueue 必须 ≥ 1，当前值: {cfg['maxQueue']}")
    if cfg["queueTimeoutMs"] < 1:
        raise ConfigError(f"配置 queueTimeoutMs 必须 ≥ 1，当前值: {cfg['queueTimeoutMs']}")
    if cfg["penaltyMs"] < 0:
        raise ConfigError(f"配置 penaltyMs 不能为负: {cfg['penaltyMs']}")
    if cfg["maxPenaltyMs"] < cfg["penaltyMs"]:
        raise ConfigError(
            f"配置 maxPenaltyMs({cfg['maxPenaltyMs']}) 不能小于 penaltyMs({cfg['penaltyMs']})"
        )
    if not isinstance(cfg["host"], str) or not cfg["host"]:
        raise ConfigError("配置 host 必须是非空字符串")
    if cfg["logLevel"] not in LOG_LEVELS:
        raise ConfigError(
            f"配置 logLevel 必须是 {'/'.join(LOG_LEVELS)} 之一，当前值: {cfg['logLevel']}"
        )
    paths = cfg["bypassPaths"]
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ConfigError("配置 bypassPaths 必须是字符串数组")
    return cfg


def load_config(
    file: Any = ...,
    env: Dict[str, str] | None = None,
    overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """载入配置。

    file 省略时读 <repo>/gate.config.json；传 None 表示完全跳过配置文件。
    """
    config_file = ROOT / "gate.config.json" if file is ... else file
    environ = os.environ if env is None else env

    cfg: Dict[str, Any] = dict(DEFAULTS)
    if config_file is not None:
        cfg.update(_read_config_file(Path(config_file)))

    for env_key, cfg_key in ENV_MAP.items():
        raw = environ.get(env_key)
        if raw is None or raw == "":
            continue
        if cfg_key in INT_KEYS:
            try:
                cfg[cfg_key] = int(str(raw).strip())
            except ValueError:
                raise ConfigError(f"环境变量 {env_key} 必须是整数，当前值: {raw}") from None
        else:
            cfg[cfg_key] = raw

    # retry.* 的环境变量：先收成一个补丁，再盖到配置文件给的 retry 上
    retry_patch: Dict[str, Any] = {}
    for env_key, retry_key in RETRY_ENV_MAP.items():
        raw = environ.get(env_key)
        if raw is None or raw == "":
            continue
        if retry_key == "cooldownSeconds":
            # 直接换成 cooldownMs，否则会被配置文件/默认值里已有的 cooldownMs 盖住
            try:
                sec = float(str(raw).strip())
            except ValueError:
                raise ConfigError(f"环境变量 {env_key} 必须是数字，当前值: {raw}") from None
            if not math.isfinite(sec) or sec < 0:
                raise ConfigError(f"环境变量 {env_key} 必须 ≥ 0，当前值: {raw}")
            retry_patch["cooldownMs"] = round(sec * 1000)
        elif retry_key in RETRY_BOOL_KEYS:
            retry_patch[retry_key] = parse_bool(raw, f"环境变量 {env_key}")
        elif retry_key in RETRY_INT_KEYS:
            try:
                retry_patch[retry_key] = int(str(raw).strip())
            except ValueError:
                raise ConfigError(f"环境变量 {env_key} 必须是整数，当前值: {raw}") from None
        else:
            retry_patch[retry_key] = raw
    if retry_patch:
        base = cfg.get("retry") if isinstance(cfg.get("retry"), dict) else {}
        cfg["retry"] = {**base, **retry_patch}

    if overrides:
        cfg.update(overrides)

    # 只保留已知字段，避免配置文件里的拼写错误被静默当成有效项
    known = {key: cfg[key] for key in DEFAULTS}
    for key in INT_KEYS:
        known[key] = _coerce_int(known[key])
    known["upstream"] = _normalize_upstream(known["upstream"])
    known["upstreams"] = normalize_upstreams(known["upstreams"])
    known["logFile"] = str(Path(ROOT, known["logFile"])) if known["logFile"] else ""
    known["limits"] = normalize_limits(known["limits"])
    known["retry"] = normalize_retry(known["retry"])

    _validate(known)
    known["effectiveLimits"] = effective_limits(known)
    return known
