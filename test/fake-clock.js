'use strict';

// 测试用假时钟：手动推进时间并按到期顺序触发定时器，
// 这样限流/冷却的断言可以精确到毫秒，不依赖真实 sleep。

class FakeClock {
  constructor(start = 100000) {
    this.t = start;
    this.timers = [];
    this.seq = 0;

    this.now = () => this.t;
    this.setTimeout = (fn, ms) => {
      const timer = {
        id: ++this.seq,
        at: this.t + Math.max(0, Number(ms) || 0),
        fn,
        cancelled: false,
      };
      this.timers.push(timer);
      return timer;
    };
    this.clearTimeout = (timer) => {
      if (timer) timer.cancelled = true;
    };
  }

  /** 直接丢给 Scheduler 的依赖注入对象。 */
  get deps() {
    return { now: this.now, setTimeout: this.setTimeout, clearTimeout: this.clearTimeout };
  }

  pendingCount() {
    return this.timers.filter((t) => !t.cancelled).length;
  }

  /** 推进 ms 毫秒；每执行一个定时器就让出一次微任务，让 await 链能跑完。 */
  async advance(ms) {
    const target = this.t + ms;
    for (;;) {
      this.timers = this.timers.filter((t) => !t.cancelled);
      let next = null;
      for (const timer of this.timers) {
        if (timer.at > target) continue;
        if (next === null || timer.at < next.at || (timer.at === next.at && timer.id < next.id)) {
          next = timer;
        }
      }
      if (!next) break;
      this.t = Math.max(this.t, next.at);
      this.timers = this.timers.filter((t) => t !== next);
      next.fn();
      await tick();
    }
    this.t = target;
    await tick();
  }
}

/** 让出一次事件循环，flush 掉所有待执行的微任务。 */
function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

module.exports = { FakeClock, tick };
