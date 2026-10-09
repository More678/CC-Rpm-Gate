'use strict';

// 用户钩子加载器：让你用一个小 JS 文件改每次 429 的重试决策，而不用改闸门代码。
//
// 钩子文件（默认 <root>/gate.hooks.js，没有就等于没配）导出 onRetry：
//
//   exports.onRetry = (ctx) => {
//     if (ctx.path === '/v1/messages' && ctx.upstream === 'myprovider') {
//       return { maxRetries: 5, mode: 'nextMinute' };
//     }
//   };
//
// 返回 undefined / null = 不干预，按配置走；返回 false = 这次不重试，把 429 透传。
// 改完文件直接保存即可，下一次 429 会按 mtime 自动重载，不用重启闸门。
//
// 约定：钩子必须是同步函数。闸门在请求线路上等它的返回值，返回 Promise 没法等，
// 会被当成非法返回值忽略（并提示一次）。

const fs = require('fs');
const path = require('path');

const { ROOT } = require('./config');

// 钩子允许覆盖的字段，以及各自的校验方式
const NUM_FIELDS = ['maxRetries', 'cooldownMs', 'waitMs', 'nextMinuteOffsetMs', 'maxHoldMs'];
const BOOL_FIELDS = ['respectRetryAfter'];
const MODES = ['cooldown', 'nextMinute'];

/**
 * 把 retry.hook 配置解析成实际文件路径。
 * - 空字符串 / false → 不启用
 * - 没带扩展名 → 补 .js（Python 版补 .py，两边各认自己的）
 * - 相对路径 → 相对仓库根目录
 */
function resolveHookPath(value, root = ROOT) {
  if (value === undefined || value === null || value === false || value === '') return '';
  const raw = String(value).trim();
  if (!raw) return '';
  const ext = path.extname(raw).toLowerCase();
  // 指定了 Python 钩子：这边不管，交给 Python 版
  if (ext === '.py') return '';
  // 写了 JS 扩展名就原样用；其余（包括 "gate.hooks" 这种 .hooks 结尾）补 .js，
  // 这样 retry.hook 写 "gate.hooks" 时两套实现各自找 .js / .py
  if (ext === '.js' || ext === '.cjs' || ext === '.mjs') return path.resolve(root, raw);
  return path.resolve(root, `${raw}.js`);
}

class UserHook {
  /**
   * @param {string} file 钩子文件绝对路径，空字符串表示不启用
   * @param {object} [deps]
   * @param {object} [deps.logger]
   * @param {boolean} [deps.required] true 时文件不存在也算错误（显式指定了却找不到）
   */
  constructor(file, deps = {}) {
    this.file = file || '';
    this.logger = deps.logger || null;
    this.required = Boolean(deps.required);

    this.fn = null;             // 当前生效的 onRetry
    this.loadedFrom = null;     // 当前 fn 来自哪次 mtime+size
    this.lastError = null;      // 最近一次加载/调用错误（给 /__gate/status 看）
    this.stats = { calls: 0, overrides: 0, forcedPassthrough: 0, errors: 0, reloads: 0 };
    this._warnedKeys = new Set(); // 同一种非法返回值只提示一次，别刷日志
  }

  /** 配了钩子文件就算启用（文件可以晚点再建）。 */
  get enabled() {
    return this.file !== '';
  }

  /** 当前文件签名；文件不存在返回 null。 */
  _stat() {
    try {
      const st = fs.statSync(this.file);
      return `${st.mtimeMs}:${st.size}`;
    } catch {
      return null;
    }
  }

  /**
   * 按 mtime 决定是否重新 require。
   * 加载失败时保留上一个能用的版本：用户存了个半截文件，不该让闸门跟着失灵。
   */
  reloadIfChanged() {
    if (!this.enabled) return;
    const sig = this._stat();
    if (sig === null) {
      if (this.fn !== null) {
        this._note(`钩子文件不见了，继续用上一次加载的版本: ${this.file}`);
      } else if (this.required && this.lastError === null) {
        this._note(`找不到钩子文件: ${this.file}`);
      }
      return;
    }
    if (sig === this.loadedFrom) return;

    try {
      delete require.cache[require.resolve(this.file)];
      const mod = require(this.file);
      const fn = typeof mod === 'function' ? mod : (mod && mod.onRetry);
      if (typeof fn !== 'function') {
        throw new Error('钩子文件要导出 onRetry 函数（exports.onRetry = (ctx) => {...}）');
      }
      this.fn = fn;
      this.loadedFrom = sig;
      this.lastError = null;
      this._warnedKeys.clear();
      this.stats.reloads += 1;
      if (this.logger) this.logger.info('已加载重试钩子', { file: this.file });
    } catch (err) {
      this.stats.errors += 1;
      this.lastError = err.message;
      // 文件签名记下来，避免同一个坏文件每次 429 都重试加载刷日志
      this.loadedFrom = sig;
      this._note(`加载钩子文件失败，${this.fn ? '继续用上一次加载的版本' : '本次忽略钩子'}: ${err.message}`);
    }
  }

  /**
   * 调用钩子，返回校验过的覆盖项。
   * @param {object} ctx 决策上下文，见 retry.js 的 decide()
   * @returns {{overrides?:object, deny?:boolean}} 空对象表示不干预
   */
  apply(ctx) {
    if (!this.enabled) return {};
    this.reloadIfChanged();
    if (!this.fn) return {};

    let out;
    this.stats.calls += 1;
    try {
      out = this.fn(ctx);
    } catch (err) {
      this.stats.errors += 1;
      this.lastError = err.message;
      this._note(`钩子抛错，这次按配置走: ${err.message}`);
      return {};
    }

    if (out === false) {
      this.stats.forcedPassthrough += 1;
      return { deny: true };
    }
    if (out === undefined || out === null || out === true) return {};
    if (typeof out !== 'object' || Array.isArray(out)) {
      this._warnOnce('type', `钩子要返回对象、false 或不返回，当前返回 ${typeof out}，本次忽略`);
      return {};
    }
    if (typeof out.then === 'function') {
      this._warnOnce('async', '钩子必须是同步函数，返回 Promise 没法等，本次忽略');
      return {};
    }

    const overrides = {};
    if (out.retry === false) {
      this.stats.forcedPassthrough += 1;
      return { deny: true };
    }
    for (const key of NUM_FIELDS) {
      if (out[key] === undefined || out[key] === null) continue;
      const n = Number(out[key]);
      if (Number.isInteger(n) && n >= 0 && (key !== 'nextMinuteOffsetMs' || n < 60000)) {
        overrides[key] = n;
      } else {
        this._warnOnce(key, `钩子返回的 ${key} 不合法（要 ≥ 0 的整数）: ${String(out[key])}，忽略这一项`);
      }
    }
    for (const key of BOOL_FIELDS) {
      if (out[key] === undefined || out[key] === null) continue;
      if (typeof out[key] === 'boolean') overrides[key] = out[key];
      else this._warnOnce(key, `钩子返回的 ${key} 要是布尔值: ${String(out[key])}，忽略这一项`);
    }
    if (out.mode !== undefined && out.mode !== null) {
      const mode = MODES.find((m) => m === String(out.mode));
      if (mode) overrides.mode = mode;
      else this._warnOnce('mode', `钩子返回的 mode 要是 ${MODES.join('/')} 之一: ${String(out.mode)}，忽略这一项`);
    }

    if (Object.keys(overrides).length > 0) this.stats.overrides += 1;
    return { overrides };
  }

  snapshot() {
    return {
      enabled: this.enabled,
      file: this.file || null,
      loaded: this.fn !== null,
      lastError: this.lastError,
      stats: { ...this.stats },
    };
  }

  _warnOnce(key, message) {
    if (this._warnedKeys.has(key)) return;
    this._warnedKeys.add(key);
    this._note(message);
  }

  _note(message) {
    if (this.logger) this.logger.warn(message, { file: this.file });
  }
}

/**
 * 按配置建用户钩子。
 * 没配 retry.hook 时看默认文件 <root>/gate.hooks.js 在不在，在就用。
 */
function createUserHook(cfg, deps = {}) {
  const raw = cfg.retry ? cfg.retry.hook : undefined;
  const explicit = raw !== undefined && raw !== null && raw !== '';
  const file = explicit
    ? resolveHookPath(raw, deps.root)
    : resolveHookPath('gate.hooks.js', deps.root);
  // 没显式配置时，文件不存在就当没这回事
  if (!explicit && file && !fs.existsSync(file)) {
    return new UserHook('', deps);
  }
  const hook = new UserHook(file, { ...deps, required: explicit });
  hook.reloadIfChanged();
  return hook;
}

module.exports = { UserHook, createUserHook, resolveHookPath, MODES, NUM_FIELDS, BOOL_FIELDS };
