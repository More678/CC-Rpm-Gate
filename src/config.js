'use strict';

// 配置加载：默认值 → gate.config.json → 环境变量，后者覆盖前者。
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

// 429 重试钩子的默认值。闸门自己扛下限流重试，客户端（Claude Code / pi）看不到 429。
const DEFAULT_RETRY = {
  enabled: true,
  maxRetries: 2,               // 闸门自己重试几次；0 = 不重试，直接透传 429
  statuses: [429, 529],        // 哪些状态码算限流
  mode: 'cooldown',            // cooldown | nextMinute
  cooldownMs: 15000,           // cooldown 模式：固定等多久
  nextMinuteOffsetMs: 1000,    // nextMinute 模式：对齐到下一分钟的第几毫秒（1000 = 第 01 秒）
  respectRetryAfter: true,     // 上游 Retry-After 比冷却更久时听上游的
  maxHoldMs: 180000,           // 单个请求累计最多被暂存多久，超了就透传 429
  maxStashed: 50,              // 同时暂存的请求数上限，超了就透传 429
  // 用户钩子文件：留空时看默认的 gate.hooks.js 在不在（Python 版看 gate.hooks.py），
  // 在就加载。钩子能按请求改这次的重试次数和等待方式，见 src/hooks.js
  hook: '',
};

const DEFAULTS = {
  port: 17530,                 // 闸门自身监听端口
  host: '127.0.0.1',           // 只监听本地，不对外暴露
  upstream: 'http://127.0.0.1:17521', // 上游（CC-Switch 本地代理）
  rpm: 20,                     // 主窗口：windowMs 内最多放行多少个请求
  windowMs: 60000,             // 主窗口长度
  // 额外的「每 X 秒最多 Y 次」限制，可写多条，必须同时满足才放行。
  // 每条写 { seconds: 10, max: 5 } 或 { windowMs: 10000, max: 5 }。
  limits: [{ seconds: 10, max: 5 }],
  maxConcurrent: 2,            // 同时在飞的请求数上限
  minGapMs: 300,               // 相邻两次放行的最小间隔
  maxQueue: 200,               // 队列长度上限，超过直接 503
  queueTimeoutMs: 600000,      // 单个请求最长排队时间
  penaltyMs: 15000,            // 上游 429/529 后的全局冷却基数
  maxPenaltyMs: 120000,        // 冷却时间上限（连续 429 会指数退避）
  bypassPaths: ['/v1/messages/count_tokens'], // 不排队直接转发的路径
  // 429 重试钩子，见 DEFAULT_RETRY
  retry: DEFAULT_RETRY,
  // 额外的命名上游：{ "名字": "https://..." }，客户端用 /__gate/u/<名字>/... 访问。
  // pi 的每个 provider 各有自己的 baseUrl，靠这个共用一套限流。
  upstreams: {},
  logFile: 'logs/gate.log',
  logLevel: 'info',            // debug | info | warn | error | silent
};

const INT_KEYS = [
  'port', 'rpm', 'windowMs', 'maxConcurrent', 'minGapMs',
  'maxQueue', 'queueTimeoutMs', 'penaltyMs', 'maxPenaltyMs',
];

const ENV_MAP = {
  GATE_PORT: 'port',
  GATE_HOST: 'host',
  GATE_UPSTREAM: 'upstream',
  GATE_UPSTREAMS: 'upstreams',   // JSON 字符串，例如 {"myprovider":"https://..."}
  GATE_RPM: 'rpm',
  GATE_WINDOW_MS: 'windowMs',
  GATE_LIMITS: 'limits',
  GATE_MAX_CONCURRENT: 'maxConcurrent',
  GATE_MIN_GAP_MS: 'minGapMs',
  GATE_MAX_QUEUE: 'maxQueue',
  GATE_QUEUE_TIMEOUT_MS: 'queueTimeoutMs',
  GATE_PENALTY_MS: 'penaltyMs',
  GATE_MAX_PENALTY_MS: 'maxPenaltyMs',
  GATE_LOG_FILE: 'logFile',
  GATE_LOG_LEVEL: 'logLevel',
};

// retry.* 的环境变量覆盖，值类型见 RETRY_ENV_KIND
const RETRY_ENV_MAP = {
  GATE_RETRY_ENABLED: 'enabled',
  GATE_RETRY_MAX: 'maxRetries',
  GATE_MAX_RETRIES: 'maxRetries',       // 别名，好记
  GATE_RETRY_STATUSES: 'statuses',
  GATE_RETRY_MODE: 'mode',
  GATE_RETRY_COOLDOWN_MS: 'cooldownMs',
  GATE_RETRY_COOLDOWN_SECONDS: 'cooldownSeconds',
  GATE_RETRY_NEXT_MINUTE_OFFSET_MS: 'nextMinuteOffsetMs',
  GATE_RETRY_RESPECT_RETRY_AFTER: 'respectRetryAfter',
  GATE_RETRY_MAX_HOLD_MS: 'maxHoldMs',
  GATE_RETRY_MAX_STASHED: 'maxStashed',
  GATE_RETRY_HOOK: 'hook',
};

const RETRY_INT_KEYS = ['maxRetries', 'cooldownMs', 'nextMinuteOffsetMs', 'maxHoldMs', 'maxStashed'];
const RETRY_BOOL_KEYS = ['enabled', 'respectRetryAfter'];
const RETRY_MODES = ['cooldown', 'nextMinute'];

const LOG_LEVELS = ['debug', 'info', 'warn', 'error', 'silent'];

/** 环境变量里的布尔值：1/true/yes/on 为真，0/false/no/off 为假。 */
function parseBool(raw, label) {
  const text = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(text)) return true;
  if (['0', 'false', 'no', 'off'].includes(text)) return false;
  throw new Error(`${label} 必须是 true/false，当前值: ${raw}`);
}

function readConfigFile(file) {
  if (!fs.existsSync(file)) return {};
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch (err) {
    throw new Error(`读取配置文件失败 ${file}: ${err.message}`);
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('配置文件顶层必须是 JSON 对象');
    }
    return parsed;
  } catch (err) {
    throw new Error(`解析配置文件失败 ${file}: ${err.message}`);
  }
}

function normalizeUpstream(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`upstream 不是合法 URL: ${value}`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`upstream 只支持 http/https: ${value}`);
  }
  // 去掉末尾斜杠，转发时直接拼接原始 path
  return url.origin + url.pathname.replace(/\/+$/, '');
}

/** 把 "10s" / "500ms" / "2m" / "10" 解析成毫秒；裸数字按秒算。 */
function parseDuration(text) {
  const m = /^\s*(\d+)\s*(ms|s|m|h)?\s*$/i.exec(String(text));
  if (!m) throw new Error(`时间窗写法不认识: ${text}（示例 10s / 500ms / 2m）`);
  const n = Number(m[1]);
  const unit = (m[2] || 's').toLowerCase();
  const factor = { ms: 1, s: 1000, m: 60000, h: 3600000 }[unit];
  const ms = n * factor;
  if (ms < 1) throw new Error(`时间窗必须 ≥ 1ms: ${text}`);
  return ms;
}

/**
 * 归一化「每 X 秒 Y 次」配置。
 * 支持三种写法：
 *   - 数组对象：[{ seconds: 10, max: 5 }, { windowMs: 1000, max: 2 }]
 *   - 字符串：  "5/10s, 2/1s"（次数/时间窗）
 *   - 单个对象：{ seconds: 10, max: 5 }
 */
function normalizeLimits(value) {
  if (value === undefined || value === null || value === '') return [];

  let entries;
  if (typeof value === 'string') {
    entries = value.split(',').map((s) => s.trim()).filter(Boolean).map((part) => {
      const [maxText, windowText] = part.split('/');
      if (windowText === undefined) {
        throw new Error(`limits 写法不认识: ${part}（示例 5/10s 表示每 10 秒 5 次）`);
      }
      const max = Number(String(maxText).trim());
      if (!Number.isInteger(max)) throw new Error(`limits 次数必须是整数: ${part}`);
      return { max, windowMs: parseDuration(windowText) };
    });
  } else if (Array.isArray(value)) {
    entries = value;
  } else if (typeof value === 'object') {
    entries = [value];
  } else {
    throw new Error(`limits 必须是数组、对象或字符串，当前类型: ${typeof value}`);
  }

  return entries.map((item, i) => {
    if (item === null || typeof item !== 'object' || Array.isArray(item)) {
      throw new Error(`limits[${i}] 必须是对象，例如 { "seconds": 10, "max": 5 }`);
    }
    const max = Number(item.max);
    if (!Number.isInteger(max) || max < 1) {
      throw new Error(`limits[${i}].max 必须是 ≥ 1 的整数，当前值: ${String(item.max)}`);
    }
    let windowMs;
    if (item.windowMs !== undefined) {
      windowMs = Number(item.windowMs);
      if (!Number.isInteger(windowMs) || windowMs < 1) {
        throw new Error(`limits[${i}].windowMs 必须是 ≥ 1 的整数，当前值: ${String(item.windowMs)}`);
      }
    } else if (item.seconds !== undefined) {
      const sec = Number(item.seconds);
      if (!Number.isFinite(sec) || sec <= 0) {
        throw new Error(`limits[${i}].seconds 必须 > 0，当前值: ${String(item.seconds)}`);
      }
      windowMs = Math.round(sec * 1000);
    } else {
      throw new Error(`limits[${i}] 需要 seconds 或 windowMs 字段`);
    }
    return { windowMs, max };
  });
}

/** 主窗口（rpm/windowMs）+ 额外 limits 合并成一张生效的限流表，去重。 */
function effectiveLimits(cfg) {
  const all = [{ windowMs: cfg.windowMs, max: cfg.rpm }, ...cfg.limits];
  const seen = new Map();
  for (const lim of all) {
    const prev = seen.get(lim.windowMs);
    // 同一个窗口写了多次，取更严格的那个
    if (prev === undefined || lim.max < prev) seen.set(lim.windowMs, lim.max);
  }
  return [...seen.entries()]
    .map(([windowMs, max]) => ({ windowMs, max }))
    .sort((a, b) => a.windowMs - b.windowMs);
}

/**
 * 归一化 retry 配置。
 * statuses 支持数组或 "429,529" 字符串；cooldownSeconds / cooldown 是 cooldownMs 的糖。
 */
function normalizeRetry(value) {
  if (value === undefined || value === null) return { ...DEFAULT_RETRY };
  if (typeof value === 'boolean') return { ...DEFAULT_RETRY, enabled: value };
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`retry 必须是对象或布尔值，当前类型: ${typeof value}`);
  }

  const out = { ...DEFAULT_RETRY, ...value };

  // cooldown / cooldownSeconds → cooldownMs
  if (value.cooldownMs === undefined) {
    if (value.cooldownSeconds !== undefined) {
      const sec = Number(value.cooldownSeconds);
      if (!Number.isFinite(sec) || sec < 0) {
        throw new Error(`retry.cooldownSeconds 必须 ≥ 0，当前值: ${String(value.cooldownSeconds)}`);
      }
      out.cooldownMs = Math.round(sec * 1000);
    } else if (value.cooldown !== undefined) {
      out.cooldownMs = parseDuration(value.cooldown);
    }
  }
  delete out.cooldownSeconds;
  delete out.cooldown;

  for (const key of RETRY_BOOL_KEYS) {
    if (typeof out[key] !== 'boolean') {
      throw new Error(`retry.${key} 必须是布尔值，当前值: ${String(out[key])}`);
    }
  }
  for (const key of RETRY_INT_KEYS) {
    const n = Number(out[key]);
    if (!Number.isInteger(n) || n < 0) {
      throw new Error(`retry.${key} 必须是 ≥ 0 的整数，当前值: ${String(out[key])}`);
    }
    out[key] = n;
  }
  if (!RETRY_MODES.includes(out.mode)) {
    throw new Error(`retry.mode 必须是 ${RETRY_MODES.join('/')} 之一，当前值: ${String(out.mode)}`);
  }
  if (out.maxStashed < 1) throw new Error(`retry.maxStashed 必须 ≥ 1，当前值: ${out.maxStashed}`);
  if (out.nextMinuteOffsetMs >= 60000) {
    throw new Error(`retry.nextMinuteOffsetMs 必须小于 60000，当前值: ${out.nextMinuteOffsetMs}`);
  }

  let statuses = out.statuses;
  if (typeof statuses === 'string') {
    statuses = statuses.split(',').map((s) => s.trim()).filter(Boolean);
  }
  if (!Array.isArray(statuses) || statuses.length === 0) {
    throw new Error('retry.statuses 必须是非空的状态码数组，例如 [429, 529]');
  }
  out.statuses = statuses.map((s) => {
    const n = Number(s);
    if (!Number.isInteger(n) || n < 100 || n > 599) {
      throw new Error(`retry.statuses 里的状态码不合法: ${String(s)}`);
    }
    return n;
  });

  // hook：false / null 当成不启用；其余必须是路径字符串
  if (out.hook === false || out.hook === null) out.hook = '';
  if (typeof out.hook !== 'string') {
    throw new Error(`retry.hook 必须是钩子文件路径字符串，当前类型: ${typeof out.hook}`);
  }
  out.hook = out.hook.trim();

  // 只保留已知字段，拼错的名字不要静默生效
  const known = {};
  for (const key of Object.keys(DEFAULT_RETRY)) known[key] = out[key];
  return known;
}

/** 归一化命名上游表：值走 normalizeUpstream 同一套校验。 */
function normalizeUpstreams(value) {
  if (value === undefined || value === null || value === '') return {};
  let raw = value;
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw);
    } catch (err) {
      throw new Error(`upstreams 不是合法 JSON: ${err.message}`);
    }
  }
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('upstreams 必须是 { "名字": "http://..." } 形式的对象');
  }
  const out = {};
  for (const [name, target] of Object.entries(raw)) {
    if (!/^[A-Za-z0-9._-]+$/.test(name)) {
      throw new Error(`upstreams 的名字只能用字母数字和 . _ -，当前值: ${name}`);
    }
    out[name] = normalizeUpstream(String(target));
  }
  return out;
}

function validate(cfg) {
  for (const key of INT_KEYS) {
    const v = cfg[key];
    if (!Number.isInteger(v)) throw new Error(`配置 ${key} 必须是整数，当前值: ${String(v)}`);
  }
  if (cfg.port < 1 || cfg.port > 65535) throw new Error(`配置 port 超出范围: ${cfg.port}`);
  if (cfg.rpm < 1) throw new Error(`配置 rpm 必须 ≥ 1，当前值: ${cfg.rpm}`);
  if (cfg.windowMs < 1) throw new Error(`配置 windowMs 必须 ≥ 1，当前值: ${cfg.windowMs}`);
  if (cfg.maxConcurrent < 1) throw new Error(`配置 maxConcurrent 必须 ≥ 1，当前值: ${cfg.maxConcurrent}`);
  if (cfg.minGapMs < 0) throw new Error(`配置 minGapMs 不能为负: ${cfg.minGapMs}`);
  if (cfg.maxQueue < 1) throw new Error(`配置 maxQueue 必须 ≥ 1，当前值: ${cfg.maxQueue}`);
  if (cfg.queueTimeoutMs < 1) throw new Error(`配置 queueTimeoutMs 必须 ≥ 1，当前值: ${cfg.queueTimeoutMs}`);
  if (cfg.penaltyMs < 0) throw new Error(`配置 penaltyMs 不能为负: ${cfg.penaltyMs}`);
  if (cfg.maxPenaltyMs < cfg.penaltyMs) {
    throw new Error(`配置 maxPenaltyMs(${cfg.maxPenaltyMs}) 不能小于 penaltyMs(${cfg.penaltyMs})`);
  }
  if (typeof cfg.host !== 'string' || cfg.host.length === 0) throw new Error('配置 host 必须是非空字符串');
  if (!LOG_LEVELS.includes(cfg.logLevel)) {
    throw new Error(`配置 logLevel 必须是 ${LOG_LEVELS.join('/')} 之一，当前值: ${cfg.logLevel}`);
  }
  if (!Array.isArray(cfg.bypassPaths) || cfg.bypassPaths.some((p) => typeof p !== 'string')) {
    throw new Error('配置 bypassPaths 必须是字符串数组');
  }
  return cfg;
}

/**
 * 载入配置。
 * @param {object} [opts]
 * @param {string} [opts.file] 配置文件路径，默认 <root>/gate.config.json
 * @param {object} [opts.env] 环境变量来源，默认 process.env
 * @param {object} [opts.overrides] 代码层覆盖（测试用）
 */
function loadConfig(opts = {}) {
  const file = opts.file === undefined ? path.join(ROOT, 'gate.config.json') : opts.file;
  const env = opts.env || process.env;

  const cfg = { ...DEFAULTS };
  if (file) Object.assign(cfg, readConfigFile(file));

  for (const [envKey, cfgKey] of Object.entries(ENV_MAP)) {
    const raw = env[envKey];
    if (raw === undefined || raw === '') continue;
    if (INT_KEYS.includes(cfgKey)) {
      const num = Number(raw);
      if (!Number.isInteger(num)) throw new Error(`环境变量 ${envKey} 必须是整数，当前值: ${raw}`);
      cfg[cfgKey] = num;
    } else {
      cfg[cfgKey] = raw;
    }
  }

  // retry.* 的环境变量：先收成一个补丁对象，再盖到配置文件给的 retry 上
  const retryPatch = {};
  for (const [envKey, retryKey] of Object.entries(RETRY_ENV_MAP)) {
    const raw = env[envKey];
    if (raw === undefined || raw === '') continue;
    if (retryKey === 'cooldownSeconds') {
      // 直接换成 cooldownMs，否则会被配置文件/默认值里已有的 cooldownMs 盖住
      const sec = Number(raw);
      if (!Number.isFinite(sec) || sec < 0) throw new Error(`环境变量 ${envKey} 必须 ≥ 0，当前值: ${raw}`);
      retryPatch.cooldownMs = Math.round(sec * 1000);
    } else if (RETRY_BOOL_KEYS.includes(retryKey)) {
      retryPatch[retryKey] = parseBool(raw, `环境变量 ${envKey}`);
    } else if (RETRY_INT_KEYS.includes(retryKey)) {
      const num = Number(raw);
      if (!Number.isInteger(num)) throw new Error(`环境变量 ${envKey} 必须是整数，当前值: ${raw}`);
      retryPatch[retryKey] = num;
    } else {
      retryPatch[retryKey] = raw;
    }
  }
  if (Object.keys(retryPatch).length > 0) {
    const base = (cfg.retry && typeof cfg.retry === 'object' && !Array.isArray(cfg.retry)) ? cfg.retry : {};
    cfg.retry = { ...base, ...retryPatch };
  }

  if (opts.overrides) Object.assign(cfg, opts.overrides);

  // 只保留已知字段，避免配置文件里的拼写错误被静默当成有效项
  const known = {};
  for (const key of Object.keys(DEFAULTS)) known[key] = cfg[key];

  known.upstream = normalizeUpstream(known.upstream);
  known.upstreams = normalizeUpstreams(known.upstreams);
  known.logFile = known.logFile ? path.resolve(ROOT, known.logFile) : '';
  known.limits = normalizeLimits(known.limits);
  known.retry = normalizeRetry(known.retry);

  validate(known);
  known.effectiveLimits = effectiveLimits(known);
  return known;
}

module.exports = {
  loadConfig,
  normalizeLimits,
  effectiveLimits,
  normalizeRetry,
  normalizeUpstreams,
  parseDuration,
  parseBool,
  DEFAULTS,
  DEFAULT_RETRY,
  RETRY_MODES,
  ROOT,
};
