'use strict';

// 全局排队调度器：所有窗口的请求都走这里，按 FIFO 一个个放行。
// 闸门条件：多条「每 X 毫秒最多 Y 次」滑动窗口 + 并发数 + 最小间隔，再叠加 429 后的全局冷却。

class Scheduler {
  /**
   * @param {object} cfg 需要 effectiveLimits（或 rpm/windowMs）、maxConcurrent、minGapMs、
   *                     maxQueue、queueTimeoutMs、penaltyMs、maxPenaltyMs
   * @param {object} [deps]
   * @param {() => number} [deps.now] 取当前时间，测试可注入
   * @param {(fn: Function, ms: number) => any} [deps.setTimeout]
   * @param {(handle: any) => void} [deps.clearTimeout]
   * @param {object} [deps.logger]
   */
  constructor(cfg, deps = {}) {
    this.cfg = cfg;
    this.now = deps.now || Date.now;
    this.setTimer = deps.setTimeout || setTimeout;
    this.clearTimer = deps.clearTimeout || clearTimeout;
    this.logger = deps.logger || null;

    // 每条限制自带一份放行时间戳队列（窗口越短的放前面，便于日志阅读）
    const limits = (cfg.effectiveLimits && cfg.effectiveLimits.length > 0)
      ? cfg.effectiveLimits
      : [{ windowMs: cfg.windowMs, max: cfg.rpm }];
    this.limits = limits
      .map((lim) => ({ windowMs: lim.windowMs, max: lim.max, hits: [] }))
      .sort((a, b) => a.windowMs - b.windowMs);

    this.queue = [];            // 等待放行的请求
    this.inFlight = 0;          // 已放行、还没回来的请求数
    this.lastDispatchAt = 0;    // 上一次放行时间，用于最小间隔
    this.penaltyUntil = 0;      // 全局冷却截止时间
    this.penaltyStreak = 0;     // 连续限流次数，用于指数退避
    this.pumpTimer = null;      // 下一次 pump 的定时器
    this.seq = 0;               // 自增票号，仅用于日志
    this.closed = false;

    this.stats = { admitted: 0, rejected: 0, timedOut: 0, penalties: 0, aborted: 0 };
  }

  /**
   * 申请一个放行名额。resolve 时返回 release 函数，调用方必须在请求结束后调用。
   * @param {object} [meta] 日志用的元信息 { window, path }
   * @returns {Promise<{release: Function, waitedMs: number, ticket: number}>}
   */
  acquire(meta = {}, opts = {}) {
    if (this.closed) return Promise.reject(makeError('闸门已关闭', 'GATE_CLOSED', 503));
    // 暂存重试回来的请求插队到前面：它已经占过一次名额，不该排到新请求后面，
    // 也不受 maxQueue 限制（拒掉一个已经等过一轮的请求比多排一个更亏）
    if (!opts.priority && this.queue.length >= this.cfg.maxQueue) {
      this.stats.rejected += 1;
      return Promise.reject(makeError(
        `排队已满（${this.queue.length}/${this.cfg.maxQueue}），请稍后重试`,
        'QUEUE_FULL',
        503,
      ));
    }

    const ticket = ++this.seq;
    const item = {
      ticket,
      meta,
      resolve: null,
      reject: null,
      enqueuedAt: this.now(),
      timer: null,
      settled: false,
      priority: Boolean(opts.priority),
    };

    const promise = new Promise((resolve, reject) => {
      item.resolve = resolve;
      item.reject = reject;
    });

    // 暂存重试要等到下一分钟，可能比 queueTimeoutMs 还长，所以允许单独放宽
    const timeoutMs = Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0
      ? opts.timeoutMs
      : this.cfg.queueTimeoutMs;
    item.timer = this.setTimer(() => {
      if (item.settled) return;
      item.settled = true;
      this.removeFromQueue(item);
      this.stats.timedOut += 1;
      if (this.logger) {
        this.logger.warn('排队超时', { ticket, waitedMs: this.now() - item.enqueuedAt, ...meta });
      }
      item.reject(makeError(
        `排队超过 ${timeoutMs}ms 仍未放行`,
        'QUEUE_TIMEOUT',
        504,
      ));
    }, timeoutMs);
    if (item.timer && typeof item.timer.unref === 'function') item.timer.unref();

    if (item.priority) {
      // 插到所有普通请求前面，但排在已有的优先请求后面（优先组内部仍是 FIFO）
      let at = 0;
      while (at < this.queue.length && this.queue[at].priority) at += 1;
      this.queue.splice(at, 0, item);
    } else {
      this.queue.push(item);
    }
    if (this.logger) {
      this.logger.debug('入队', {
        ticket, queued: this.queue.length, inFlight: this.inFlight, priority: item.priority, ...meta,
      });
    }

    // 客户端断开时调用方直接调 promise.cancel()：立刻出队，不占名额
    promise.cancel = () => this.cancel(item);
    this.pump();
    return promise;
  }

  /** 客户端断开 / 主动放弃排队：出队并拒绝，不计入放行。 */
  cancel(item) {
    if (!item || item.settled) return false;
    item.settled = true;
    if (item.timer) this.clearTimer(item.timer);
    this.removeFromQueue(item);
    this.stats.aborted += 1;
    if (this.logger) {
      this.logger.debug('排队中断（客户端断开）', { ticket: item.ticket, ...item.meta });
    }
    item.reject(makeError('客户端在排队期间断开', 'CLIENT_ABORTED', 499));
    return true;
  }

  removeFromQueue(item) {
    const idx = this.queue.indexOf(item);
    if (idx !== -1) this.queue.splice(idx, 1);
  }

  /**
   * 上游返回 429/529 时调用：进入全局冷却，连续触发则指数退避。
   * @param {number} [retryAfterMs] 上游 Retry-After 换算的毫秒
   * @param {object} [opts]
   * @param {number} [opts.exactMs] 精确冷却时长：不做指数退避、不受 maxPenaltyMs 削顶。
   *                                429 暂存用它把「等到下一分钟 00/01 秒」原样落到冷却上。
   */
  penalize(retryAfterMs, opts = {}) {
    this.penaltyStreak += 1;
    this.stats.penalties += 1;

    if (Number.isFinite(opts.exactMs)) {
      const wait = Math.max(0, opts.exactMs);
      const until = this.now() + wait;
      if (until > this.penaltyUntil) this.penaltyUntil = until;
      if (this.logger) {
        this.logger.warn('上游限流，按暂存时长冷却', { waitMs: wait, streak: this.penaltyStreak });
      }
      this.pump();
      return wait;
    }

    const backoff = this.cfg.penaltyMs * Math.pow(2, this.penaltyStreak - 1);
    const base = Math.min(backoff, this.cfg.maxPenaltyMs);
    // 上游明确给了 Retry-After 就听它的，但不超过上限
    const wait = Math.min(
      Math.max(base, Number.isFinite(retryAfterMs) ? retryAfterMs : 0),
      this.cfg.maxPenaltyMs,
    );
    const until = this.now() + wait;
    if (until > this.penaltyUntil) this.penaltyUntil = until;
    if (this.logger) {
      this.logger.warn('上游限流，进入冷却', { waitMs: wait, streak: this.penaltyStreak });
    }
    this.pump();
    return wait;
  }

  /** 上游正常返回时调用：重置退避计数。 */
  onSuccess() {
    this.penaltyStreak = 0;
  }

  /** 距离下一次可放行还要等多少毫秒；0 表示现在就能放，Infinity 表示并发打满。 */
  nextDelayMs() {
    const now = this.now();
    this.trimWindows(now);

    if (this.inFlight >= this.cfg.maxConcurrent) return Infinity; // 等 release 事件触发

    const waits = [];
    if (now < this.penaltyUntil) waits.push(this.penaltyUntil - now);
    for (const lim of this.limits) {
      if (lim.hits.length >= lim.max) {
        // 最早那次放行滚出窗口时，才腾出一个名额
        waits.push(lim.hits[0] + lim.windowMs - now);
      }
    }
    if (this.cfg.minGapMs > 0 && this.lastDispatchAt > 0) {
      const gap = this.lastDispatchAt + this.cfg.minGapMs - now;
      if (gap > 0) waits.push(gap);
    }
    if (waits.length === 0) return 0;
    return Math.max(0, Math.max(...waits));
  }

  /** 推动队列：能放就放，不能放就定时重试。 */
  pump() {
    if (this.pumpTimer) {
      this.clearTimer(this.pumpTimer);
      this.pumpTimer = null;
    }
    if (this.closed) return;

    while (this.queue.length > 0) {
      const delay = this.nextDelayMs();
      if (delay !== 0) {
        // Infinity 表示并发打满，由 release() 重新触发 pump，不需要定时器
        if (Number.isFinite(delay)) {
          this.pumpTimer = this.setTimer(() => {
            this.pumpTimer = null;
            this.pump();
          }, Math.max(delay, 1));
          if (this.pumpTimer && typeof this.pumpTimer.unref === 'function') this.pumpTimer.unref();
        }
        return;
      }
      this.dispatch(this.queue.shift());
    }
  }

  /** 放行一个请求。 */
  dispatch(item) {
    if (!item || item.settled) return;
    item.settled = true;
    if (item.timer) this.clearTimer(item.timer);

    const now = this.now();
    for (const lim of this.limits) lim.hits.push(now);
    this.lastDispatchAt = now;
    this.inFlight += 1;
    this.stats.admitted += 1;

    const waitedMs = now - item.enqueuedAt;
    if (this.logger) {
      this.logger.debug('放行', {
        ticket: item.ticket,
        waitedMs,
        inFlight: this.inFlight,
        queued: this.queue.length,
        ...item.meta,
      });
    }

    let released = false;
    const release = () => {
      if (released) return; // 幂等，避免并发数被重复扣减
      released = true;
      this.inFlight -= 1;
      this.pump();
    };

    item.resolve({ release, waitedMs, ticket: item.ticket });
  }

  /** 丢掉各窗口外的历史记录。 */
  trimWindows(now = this.now()) {
    for (const lim of this.limits) {
      const cutoff = now - lim.windowMs;
      while (lim.hits.length > 0 && lim.hits[0] <= cutoff) lim.hits.shift();
    }
  }

  snapshot() {
    const now = this.now();
    this.trimWindows(now);
    return {
      queued: this.queue.length,
      queuedPriority: this.queue.reduce((n, it) => n + (it.priority ? 1 : 0), 0),
      inFlight: this.inFlight,
      maxConcurrent: this.cfg.maxConcurrent,
      minGapMs: this.cfg.minGapMs,
      limits: this.limits.map((lim) => ({
        windowMs: lim.windowMs,
        max: lim.max,
        used: lim.hits.length,
        // 该窗口下一个名额还要等多久
        resetInMs: lim.hits.length >= lim.max ? Math.max(0, lim.hits[0] + lim.windowMs - now) : 0,
      })),
      penaltyMsLeft: Math.max(0, this.penaltyUntil - now),
      penaltyStreak: this.penaltyStreak,
      stats: { ...this.stats },
    };
  }

  /** 关闭调度器，把还在排队的请求全部拒掉。 */
  close() {
    this.closed = true;
    if (this.pumpTimer) {
      this.clearTimer(this.pumpTimer);
      this.pumpTimer = null;
    }
    const pending = this.queue.splice(0, this.queue.length);
    for (const item of pending) {
      if (item.settled) continue;
      item.settled = true;
      if (item.timer) this.clearTimer(item.timer);
      item.reject(makeError('闸门正在关闭', 'GATE_CLOSED', 503));
    }
  }
}

function makeError(message, code, status) {
  const err = new Error(message);
  err.code = code;
  err.status = status;
  return err;
}

module.exports = { Scheduler, makeError };
