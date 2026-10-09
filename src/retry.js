'use strict';

// 429 重试钩子：上游限流时，决定「闸门自己扛下来重试」还是「把 429 透传给客户端」。
//
// 两种暂存模式（mode）：
//   - cooldown：  固定冷却 cooldownMs 毫秒后重试（若上游给了更大的 Retry-After 就听它的）
//   - nextMinute：暂存到「下一分钟的第 00/01 秒」再重试，对齐按分钟重置的配额
//
// 钩子是可替换的：server.js 把它注入给 proxy，测试里可以注入假时钟。
// 注意：nextMinute 必须用墙上时钟（Date.now），不能用单调时钟，否则对不上真实的分钟边界。
//
// 决策参数的优先级（低 → 高）：gate.config.json 的 retry.* → 请求头
// x-gate-max-retries / x-gate-retry-mode → 用户钩子文件（src/hooks.js）。
// 钩子能按请求改重试次数和等待方式，改完保存即生效，不用重启闸门。

const { createUserHook } = require('./hooks');

const MINUTE_MS = 60000;
const MODES = ['cooldown', 'nextMinute'];

/**
 * 距离下一次「分钟边界 + offsetMs」（比如 :01）还有多少毫秒，结果永远 > 0。
 * :00.3 收到 429 时，本分钟的 :01 还没到，就等它（多半是上游时钟还没翻页）；
 * 过了 :01 才等下一分钟的 :01。
 */
function nextMinuteWaitMs(nowMs, offsetMs = 0) {
  const sinceMinute = ((nowMs % MINUTE_MS) + MINUTE_MS) % MINUTE_MS;
  if (sinceMinute < offsetMs) return offsetMs - sinceMinute;
  return MINUTE_MS - sinceMinute + offsetMs;
}

function pick(value) {
  if (value === undefined || value === null) return '';
  return Array.isArray(value) ? String(value[0]) : String(value);
}

/** 解析单个请求上的覆盖头：x-gate-max-retries / x-gate-retry-mode。 */
function requestOverrides(headers = {}) {
  const out = {};
  const rawMax = pick(headers['x-gate-max-retries']).trim();
  if (rawMax !== '') {
    const n = Number(rawMax);
    // 非法值直接忽略，不能让一个坏头把请求打挂
    if (Number.isInteger(n) && n >= 0) out.maxRetries = n;
  }
  const rawMode = pick(headers['x-gate-retry-mode']).trim();
  if (rawMode) {
    const mode = MODES.find((m) => m.toLowerCase() === rawMode.toLowerCase());
    if (mode) out.mode = mode;
  }
  return out;
}

class RetryHook {
  /**
   * @param {object} retryCfg loadConfig().retry
   * @param {object} [deps]
   * @param {() => number} [deps.wallNow] 墙上时钟，nextMinute 要用它对齐分钟边界
   * @param {object} [deps.logger]
   * @param {object} [deps.userHook] 用户钩子，见 src/hooks.js
   */
  constructor(retryCfg, deps = {}) {
    this.cfg = retryCfg;
    this.wallNow = deps.wallNow || Date.now;
    this.logger = deps.logger || null;
    // 用户钩子（src/hooks.js 的 UserHook），没配就是 null
    this.userHook = deps.userHook || null;

    this.stashed = 0;          // 当前正在暂存等待的请求数
    this.releaseAt = [];       // 暂存中各请求的预计放行时刻（墙上时钟）
    this.stats = {
      retried: 0,              // 闸门自己发起的重试次数
      stashed: 0,              // 进入暂存的次数
      recovered: 0,            // 重试后拿到非限流响应的次数
      exhausted: 0,            // 重试次数用尽，最终把 429 透传
      passedThrough: 0,        // 未重试直接透传（含关闭、暂存满、超预算）
      peakStashed: 0,
      heldMsTotal: 0,          // 累计暂存等待时长
    };
  }

  /** 这次请求允许重试几回（支持按请求头覆盖）。 */
  maxRetriesFor(overrides = {}) {
    const n = overrides.maxRetries;
    return Number.isInteger(n) && n >= 0 ? n : this.cfg.maxRetries;
  }

  modeFor(overrides = {}) {
    return overrides.mode || this.cfg.mode;
  }

  /** 配了用户钩子时，钩子可能把重试次数从 0 调上去，所以请求体得先缓存好。 */
  hasUserHook() {
    return Boolean(this.userHook && this.userHook.enabled);
  }

  /**
   * 拿到上游状态码后决定下一步。
   * 优先级（低 → 高）：配置 → 请求头覆盖 → 用户钩子。
   * @param {object} ctx
   * @param {number} ctx.status 上游状态码
   * @param {number} ctx.attempt 已经发出去的次数（首次 = 1）
   * @param {number} ctx.heldMs 这个请求到目前为止累计被暂存了多久
   * @param {number} [ctx.retryAfterMs] 上游 Retry-After 换算出的毫秒
   * @param {object} [ctx.overrides] requestOverrides() 的结果
   * @param {object} [ctx.meta] 请求信息（path/method/upstream/window），原样交给用户钩子
   * @returns {{action:'retry'|'passthrough', waitMs?:number, mode?:string, reason?:string}}
   */
  decide(ctx) {
    const { status, attempt, heldMs = 0, retryAfterMs, overrides = {}, meta = {} } = ctx;
    if (!this.cfg.enabled) return this._pass('disabled');
    if (!this.cfg.statuses.includes(status)) return { action: 'passthrough', reason: 'not-retryable' };

    const hint = Number.isFinite(retryAfterMs) && retryAfterMs > 0 ? retryAfterMs : 0;
    // 本次生效的参数：先配置，再叠请求头
    const eff = {
      maxRetries: this.maxRetriesFor(overrides),
      mode: this.modeFor(overrides),
      cooldownMs: this.cfg.cooldownMs,
      nextMinuteOffsetMs: this.cfg.nextMinuteOffsetMs,
      respectRetryAfter: this.cfg.respectRetryAfter,
      maxHoldMs: this.cfg.maxHoldMs,
    };

    // 再叠用户钩子
    let forcedWaitMs = null;
    if (this.userHook) {
      const hooked = this.userHook.apply({
        status,
        attempt,
        heldMs,
        retryAfterMs: hint || null,
        method: meta.method || null,
        path: meta.path || null,
        upstream: meta.upstream || null,
        window: meta.window || null,
        stashed: this.stashed,
        ...eff,
      });
      if (hooked.deny) return this._pass('hook');
      if (hooked.overrides) {
        const { waitMs, ...rest } = hooked.overrides;
        Object.assign(eff, rest);
        if (waitMs !== undefined) forcedWaitMs = waitMs;
      }
    }

    if (eff.maxRetries < 1) return this._pass('retry-disabled');
    if (attempt > eff.maxRetries) return this._pass('max-retries');
    if (this.stashed >= this.cfg.maxStashed) return this._pass('stash-full');

    const now = this.wallNow();
    let waitMs;
    if (forcedWaitMs !== null) {
      // 钩子直接给了等待时长：照办，不再叠 Retry-After
      waitMs = forcedWaitMs;
    } else if (eff.mode === 'nextMinute') {
      waitMs = nextMinuteWaitMs(now, eff.nextMinuteOffsetMs);
      // 上游要求等更久：从它指定的时刻再往后对齐到下一个分钟边界
      if (hint > waitMs) waitMs = nextMinuteWaitMs(now + hint, eff.nextMinuteOffsetMs) + hint;
    } else {
      waitMs = eff.respectRetryAfter ? Math.max(eff.cooldownMs, hint) : eff.cooldownMs;
    }

    // 总暂存预算：别让一个请求在闸门里卡到天荒地老
    if (eff.maxHoldMs > 0 && heldMs + waitMs > eff.maxHoldMs) {
      return this._pass('hold-budget');
    }
    return { action: 'retry', waitMs, mode: eff.mode };
  }

  _pass(reason) {
    this.stats.passedThrough += 1;
    if (reason === 'max-retries') this.stats.exhausted += 1;
    return { action: 'passthrough', reason };
  }

  /** 开始暂存等待。 */
  enter(waitMs) {
    this.stashed += 1;
    this.stats.stashed += 1;
    if (this.stashed > this.stats.peakStashed) this.stats.peakStashed = this.stashed;
    const at = this.wallNow() + waitMs;
    this.releaseAt.push(at);
    return at;
  }

  /** 暂存等待结束（无论是正常醒来还是客户端断开）。 */
  leave(releaseAt, waitedMs = 0) {
    if (this.stashed > 0) this.stashed -= 1;
    const idx = this.releaseAt.indexOf(releaseAt);
    if (idx !== -1) this.releaseAt.splice(idx, 1);
    this.stats.heldMsTotal += Math.max(0, Math.round(waitedMs));
  }

  /** 真的重新发出去一次。 */
  recordRetry() {
    this.stats.retried += 1;
  }

  /** 重试之后拿到了非限流响应。 */
  recordRecovered() {
    this.stats.recovered += 1;
  }

  snapshot() {
    const now = this.wallNow();
    const next = this.releaseAt.length > 0 ? Math.min(...this.releaseAt) : null;
    return {
      enabled: this.cfg.enabled,
      mode: this.cfg.mode,
      maxRetries: this.cfg.maxRetries,
      cooldownMs: this.cfg.cooldownMs,
      nextMinuteOffsetMs: this.cfg.nextMinuteOffsetMs,
      maxHoldMs: this.cfg.maxHoldMs,
      maxStashed: this.cfg.maxStashed,
      statuses: [...this.cfg.statuses],
      stashed: this.stashed,
      nextReleaseInMs: next === null ? 0 : Math.max(0, Math.round(next - now)),
      stats: { ...this.stats },
      hook: this.userHook ? this.userHook.snapshot() : { enabled: false },
    };
  }
}

/**
 * 建重试钩子。没显式传 userHook 时按配置自动加载用户钩子文件。
 * 传 userHook: null 可以明确关掉（测试用）。
 */
function createRetryHook(cfg, deps = {}) {
  const resolved = { ...deps };
  if (!('userHook' in deps)) {
    resolved.userHook = createUserHook(cfg, { logger: deps.logger });
  }
  return new RetryHook(cfg.retry, resolved);
}

module.exports = {
  RetryHook,
  createRetryHook,
  nextMinuteWaitMs,
  requestOverrides,
  MODES,
  MINUTE_MS,
};
