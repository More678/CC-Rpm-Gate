'use strict';

// 用户重试钩子测试：加载/热重载、返回值校验、与配置和请求头的优先级、端到端改重试次数。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');

const { UserHook, createUserHook, resolveHookPath } = require('../src/hooks');
const { RetryHook } = require('../src/retry');
const { Scheduler } = require('../src/scheduler');
const { createHandler } = require('../src/proxy');
const { DEFAULT_RETRY, normalizeRetry, loadConfig } = require('../src/config');

const silentLogger = { debug() {}, info() {}, warn() {}, error() {}, close() {} };

/** 建个临时目录放钩子文件，返回 { dir, write(name, code), cleanup() }。 */
function tempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-hooks-'));
  let seq = 0;
  return {
    dir,
    /** 写钩子文件。每次换个文件名里的序号也行，但这里靠 mtime 变化触发重载。 */
    write(name, code) {
      const file = path.join(dir, name);
      fs.writeFileSync(file, code, 'utf8');
      // 保证 mtime 一定变化：同一毫秒内连写两次时，文件系统时间戳可能一样
      seq += 1;
      const future = new Date(Date.now() + seq * 1000);
      fs.utimesSync(file, future, future);
      return file;
    },
    cleanup() {
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

function makeHook(retryOverrides, userHook) {
  return new RetryHook({ ...DEFAULT_RETRY, ...retryOverrides }, {
    wallNow: () => 0,
    logger: silentLogger,
    userHook,
  });
}

test('resolveHookPath：补扩展名、认相对路径、别的语言不管', () => {
  const root = path.resolve('/tmp/root');
  assert.equal(resolveHookPath('', root), '', '空字符串 = 不启用');
  assert.equal(resolveHookPath(false, root), '');
  assert.equal(resolveHookPath(null, root), '');
  assert.equal(resolveHookPath('gate.hooks', root), path.join(root, 'gate.hooks.js'), '没 js 扩展名就补 .js');
  assert.equal(resolveHookPath('myhook', root), path.join(root, 'myhook.js'));
  assert.equal(resolveHookPath('a/b.js', root), path.join(root, 'a', 'b.js'), '相对仓库根目录');
  assert.equal(resolveHookPath('a/b.cjs', root), path.join(root, 'a', 'b.cjs'), '.cjs 也认');
  assert.equal(resolveHookPath('gate.hooks.py', root), '', '.py 交给 Python 版，这边不认');
  const abs = path.resolve('/elsewhere/h.js');
  assert.equal(resolveHookPath(abs, root), abs, '绝对路径原样用');
});

test('没配钩子、默认文件也不存在时，钩子不启用', () => {
  const tmp = tempDir();
  try {
    const hook = createUserHook({ retry: { ...DEFAULT_RETRY } }, { root: tmp.dir, logger: silentLogger });
    assert.equal(hook.enabled, false);
    assert.deepEqual(hook.apply({ status: 429 }), {}, '不启用时调用是空操作');
    assert.deepEqual(hook.snapshot(), { enabled: false, file: null, loaded: false, lastError: null, stats: hook.stats });
  } finally {
    tmp.cleanup();
  }
});

test('默认文件 gate.hooks.js 存在就自动加载', () => {
  const tmp = tempDir();
  try {
    tmp.write('gate.hooks.js', 'exports.onRetry = () => ({ maxRetries: 7 });');
    const hook = createUserHook({ retry: { ...DEFAULT_RETRY } }, { root: tmp.dir, logger: silentLogger });
    assert.equal(hook.enabled, true);
    assert.equal(hook.snapshot().loaded, true);
    assert.deepEqual(hook.apply({ status: 429 }), { overrides: { maxRetries: 7 } });
  } finally {
    tmp.cleanup();
  }
});

test('钩子改重试次数：0 次也能被钩子调上去', () => {
  const tmp = tempDir();
  try {
    const file = tmp.write('h.js', 'exports.onRetry = (ctx) => ctx.path === "/v1/messages" ? { maxRetries: 3 } : null;');
    const user = new UserHook(file, { logger: silentLogger });
    const h = makeHook({ maxRetries: 0, cooldownMs: 1000 }, user);

    assert.equal(h.hasUserHook(), true, '配了钩子就要提前缓存请求体');
    const yes = h.decide({ status: 429, attempt: 1, meta: { path: '/v1/messages' } });
    assert.deepEqual(yes, { action: 'retry', waitMs: 1000, mode: 'cooldown' }, '配置 0 次，钩子给到 3 次');

    const no = h.decide({ status: 429, attempt: 1, meta: { path: '/v1/other' } });
    assert.equal(no.action, 'passthrough');
    assert.equal(no.reason, 'retry-disabled', '钩子不干预时仍按配置的 0 次透传');
  } finally {
    tmp.cleanup();
  }
});

test('钩子优先级高于请求头，请求头优先级高于配置', () => {
  const tmp = tempDir();
  try {
    const file = tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 9, mode: "nextMinute" });');
    const user = new UserHook(file, { logger: silentLogger });

    // 配置 1 次 / cooldown；请求头要 2 次；钩子要 9 次 + nextMinute
    const h = makeHook({ maxRetries: 1, mode: 'cooldown', nextMinuteOffsetMs: 1000 }, user);
    const d = h.decide({ status: 429, attempt: 3, overrides: { maxRetries: 2, mode: 'cooldown' } });
    assert.equal(d.action, 'retry', '第 3 次仍允许：钩子把上限提到 9');
    assert.equal(d.mode, 'nextMinute', '钩子的 mode 盖掉请求头的 cooldown');
    assert.equal(d.waitMs, 1000, 'wallNow=0（正好 :00）→ 等本分钟的 :01');

    // 钩子不干预时，请求头盖配置
    const quiet = makeHook({ maxRetries: 1 }, new UserHook(tmp.write('q.js', 'exports.onRetry = () => null;'), { logger: silentLogger }));
    assert.equal(quiet.decide({ status: 429, attempt: 2, overrides: { maxRetries: 2 } }).action, 'retry');
    assert.equal(quiet.decide({ status: 429, attempt: 3, overrides: { maxRetries: 2 } }).reason, 'max-retries');
  } finally {
    tmp.cleanup();
  }
});

test('钩子返回 false / { retry: false } 强制透传', () => {
  const tmp = tempDir();
  try {
    for (const code of ['exports.onRetry = () => false;', 'exports.onRetry = () => ({ retry: false });']) {
      const user = new UserHook(tmp.write(`deny-${code.length}.js`, code), { logger: silentLogger });
      const h = makeHook({ maxRetries: 5 }, user);
      const d = h.decide({ status: 429, attempt: 1 });
      assert.equal(d.action, 'passthrough');
      assert.equal(d.reason, 'hook');
      assert.equal(user.stats.forcedPassthrough, 1);
      assert.equal(h.stats.passedThrough, 1);
      assert.equal(h.stats.exhausted, 0, '钩子拦下的不算次数用尽');
    }
  } finally {
    tmp.cleanup();
  }
});

test('钩子给 waitMs 时直接照办，不再叠 Retry-After', () => {
  const tmp = tempDir();
  try {
    const user = new UserHook(tmp.write('h.js', 'exports.onRetry = () => ({ waitMs: 2500 });'), { logger: silentLogger });
    const h = makeHook({ maxRetries: 5, cooldownMs: 1000, maxHoldMs: 10000 }, user);
    assert.equal(h.decide({ status: 429, attempt: 1, retryAfterMs: 90000 }).waitMs, 2500);
  } finally {
    tmp.cleanup();
  }
});

test('钩子改不了「哪些状态码算限流」', () => {
  const tmp = tempDir();
  try {
    const user = new UserHook(tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 5 });'), { logger: silentLogger });
    const h = makeHook({ maxRetries: 2, statuses: [429] }, user);
    const d = h.decide({ status: 503, attempt: 1 });
    assert.equal(d.reason, 'not-retryable');
    assert.equal(user.stats.calls, 0, '非限流状态码不该调钩子');
  } finally {
    tmp.cleanup();
  }
});

test('钩子抛错 / 返回非法值：记一笔，然后按配置走', () => {
  const tmp = tempDir();
  try {
    const bad = new UserHook(tmp.write('throw.js', 'exports.onRetry = () => { throw new Error("boom"); };'), { logger: silentLogger });
    const h = makeHook({ maxRetries: 2, cooldownMs: 1000 }, bad);
    assert.deepEqual(h.decide({ status: 429, attempt: 1 }), { action: 'retry', waitMs: 1000, mode: 'cooldown' });
    assert.equal(bad.stats.errors, 1);
    assert.equal(bad.lastError, 'boom');
    assert.equal(bad.snapshot().loaded, true, '抛错不影响「已加载」状态');

    // 非法字段逐项忽略，合法字段照用
    const mixed = new UserHook(
      tmp.write('mixed.js', 'exports.onRetry = () => ({ maxRetries: -1, mode: "whatever", cooldownMs: 2000 });'),
      { logger: silentLogger });
    const h2 = makeHook({ maxRetries: 2, mode: 'cooldown', cooldownMs: 1000 }, mixed);
    const d2 = h2.decide({ status: 429, attempt: 1 });
    assert.equal(d2.waitMs, 2000, '合法的 cooldownMs 生效');
    assert.equal(d2.mode, 'cooldown', '非法 mode 被忽略，回到配置值');
    assert.equal(d2.action, 'retry', '非法 maxRetries 被忽略，用配置的 2');

    // 返回 Promise：同步拿不到，忽略
    const asyncHook = new UserHook(tmp.write('async.js', 'exports.onRetry = async () => ({ maxRetries: 9 });'), { logger: silentLogger });
    const h3 = makeHook({ maxRetries: 1, cooldownMs: 1000 }, asyncHook);
    assert.equal(h3.decide({ status: 429, attempt: 2 }).reason, 'max-retries', '异步钩子不生效，按配置的 1 次');
  } finally {
    tmp.cleanup();
  }
});

test('热重载：改文件下一次就生效，坏文件保留上一个好版本', () => {
  const tmp = tempDir();
  try {
    const file = tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 2 });');
    const user = new UserHook(file, { logger: silentLogger });
    user.reloadIfChanged();
    assert.deepEqual(user.apply({ status: 429 }), { overrides: { maxRetries: 2 } });
    assert.equal(user.stats.reloads, 1);

    tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 8 });');
    assert.deepEqual(user.apply({ status: 429 }), { overrides: { maxRetries: 8 } }, '改完不用重启');
    assert.equal(user.stats.reloads, 2);

    // 存了个语法错误的半截文件：继续用上一个版本，并记下错误
    tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries:');
    assert.deepEqual(user.apply({ status: 429 }), { overrides: { maxRetries: 8 } }, '坏文件不让闸门失灵');
    assert.equal(user.stats.errors, 1);
    assert.match(user.snapshot().lastError, /Unexpected|Invalid|SyntaxError|token/i);

    // 修好了：重新生效
    tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 4 });');
    assert.deepEqual(user.apply({ status: 429 }), { overrides: { maxRetries: 4 } });
  } finally {
    tmp.cleanup();
  }
});

test('钩子文件没导出 onRetry 就算加载失败', () => {
  const tmp = tempDir();
  try {
    const user = new UserHook(tmp.write('h.js', 'exports.somethingElse = 1;'), { logger: silentLogger });
    user.reloadIfChanged();
    assert.equal(user.snapshot().loaded, false);
    assert.match(user.lastError, /onRetry/);
    assert.deepEqual(user.apply({ status: 429 }), {}, '加载失败就当没钩子');
  } finally {
    tmp.cleanup();
  }
});

test('钩子拿到的 ctx 带请求信息和本次生效值', () => {
  const tmp = tempDir();
  try {
    const dump = path.join(tmp.dir, 'ctx.json');
    const user = new UserHook(
      tmp.write('h.js', `exports.onRetry = (ctx) => { require('fs').writeFileSync(${JSON.stringify(dump)}, JSON.stringify(ctx)); };`),
      { logger: silentLogger });
    const h = makeHook({ maxRetries: 2, mode: 'cooldown', cooldownMs: 1000 }, user);
    h.decide({
      status: 429,
      attempt: 2,
      heldMs: 1500,
      retryAfterMs: 3000,
      overrides: { maxRetries: 4 },
      meta: { method: 'POST', path: '/v1/messages', upstream: 'myprovider', window: 'cc-1' },
    });
    const ctx = JSON.parse(fs.readFileSync(dump, 'utf8'));
    assert.equal(ctx.status, 429);
    assert.equal(ctx.attempt, 2);
    assert.equal(ctx.heldMs, 1500);
    assert.equal(ctx.retryAfterMs, 3000);
    assert.equal(ctx.method, 'POST');
    assert.equal(ctx.path, '/v1/messages');
    assert.equal(ctx.upstream, 'myprovider');
    assert.equal(ctx.window, 'cc-1');
    assert.equal(ctx.stashed, 0);
    assert.equal(ctx.maxRetries, 4, 'ctx 里是请求头覆盖后的生效值');
    assert.equal(ctx.mode, 'cooldown');
    assert.equal(ctx.cooldownMs, 1000);
    assert.equal(ctx.respectRetryAfter, true);
  } finally {
    tmp.cleanup();
  }
});

test('retry.hook 配置与 GATE_RETRY_HOOK 环境变量', () => {
  assert.equal(normalizeRetry({}).hook, '', '默认不配钩子');
  assert.equal(normalizeRetry({ hook: '  my.js  ' }).hook, 'my.js', '去掉首尾空白');
  assert.equal(normalizeRetry({ hook: false }).hook, '', 'false = 不启用');
  assert.throws(() => normalizeRetry({ hook: 42 }), /retry\.hook 必须是钩子文件路径字符串/);

  const cfg = loadConfig({ file: null, env: { GATE_RETRY_HOOK: 'env.js' } });
  assert.equal(cfg.retry.hook, 'env.js');
});

test('snapshot：/__gate/status 能看到钩子状态和计数', () => {
  const tmp = tempDir();
  try {
    const user = new UserHook(tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 3 });'), { logger: silentLogger });
    const h = makeHook({ maxRetries: 1 }, user);
    h.decide({ status: 429, attempt: 1 });
    const snap = h.snapshot();
    assert.equal(snap.hook.enabled, true);
    assert.equal(snap.hook.loaded, true);
    assert.equal(snap.hook.stats.calls, 1);
    assert.equal(snap.hook.stats.overrides, 1);
    assert.equal(snap.hook.lastError, null);

    const none = makeHook({}, null).snapshot();
    assert.deepEqual(none.hook, { enabled: false }, '没钩子时只报一个 enabled:false');
  } finally {
    tmp.cleanup();
  }
});

test('端到端：配置 0 次重试，钩子调到 2 次，客户端只看到 200', async () => {
  const tmp = tempDir();
  const servers = [];
  const listen = (handler) => new Promise((resolve) => {
    const server = http.createServer(handler);
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    server.listen(0, '127.0.0.1', () => {
      const url = `http://127.0.0.1:${server.address().port}`;
      servers.push(server);
      resolve({ server, url });
    });
  });

  try {
    let hits = 0;
    const bodies = [];
    const upstream = await listen((req, res) => {
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        hits += 1;
        bodies.push(Buffer.concat(chunks).toString('utf8'));
        if (hits <= 2) {
          res.writeHead(429, { 'content-type': 'application/json' });
          res.end('{"type":"error","error":{"type":"rate_limit_error"}}');
          return;
        }
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, attempt: hits }));
      });
    });

    const file = tmp.write('h.js', 'exports.onRetry = () => ({ maxRetries: 2, waitMs: 20 });');
    const cfg = {
      host: '127.0.0.1', port: 0, upstream: upstream.url,
      rpm: 1000, windowMs: 60000, limits: [], effectiveLimits: [{ windowMs: 60000, max: 1000 }],
      maxConcurrent: 10, minGapMs: 0, maxQueue: 100, queueTimeoutMs: 5000,
      penaltyMs: 10, maxPenaltyMs: 100, bypassPaths: [], upstreams: {},
      // 配置里明确 0 次：没有钩子的话 429 会直接透传
      retry: { ...DEFAULT_RETRY, maxRetries: 0, maxHoldMs: 5000 },
      logFile: '', logLevel: 'silent',
    };
    const scheduler = new Scheduler(cfg, { logger: silentLogger });
    const retryHook = new RetryHook(cfg.retry, {
      logger: silentLogger,
      userHook: new UserHook(file, { logger: silentLogger }),
    });
    const gate = await listen(createHandler(cfg, scheduler, silentLogger, { retryHook }));

    const payload = JSON.stringify({ model: 'x', messages: [{ role: 'user', content: 'hi' }] });
    const res = await new Promise((resolve, reject) => {
      const t = new URL(`${gate.url}/v1/messages`);
      const req = http.request({
        hostname: t.hostname, port: t.port, path: t.pathname, method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) },
      }, (r) => {
        const chunks = [];
        r.on('data', (c) => chunks.push(c));
        r.on('end', () => resolve({ status: r.statusCode, body: Buffer.concat(chunks).toString('utf8') }));
      });
      req.on('error', reject);
      req.end(payload);
    });

    assert.equal(res.status, 200, '客户端看不到 429');
    assert.equal(JSON.parse(res.body).attempt, 3);
    assert.equal(hits, 3, '上游被打了 3 次（1 次首发 + 2 次重试）');
    assert.deepEqual(new Set(bodies), new Set([payload]), '重放的请求体和原始一致');
    assert.equal(retryHook.stats.retried, 2);
    assert.equal(retryHook.stats.recovered, 1);
    scheduler.close();
  } finally {
    for (const s of servers) {
      s.closeAllConnections?.();
      await new Promise((done) => s.close(done));
    }
    tmp.cleanup();
  }
});
