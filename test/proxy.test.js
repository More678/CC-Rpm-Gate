'use strict';

// 代理层端到端测试：起一个假上游 + 真闸门，用真实 HTTP 跑。
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { Scheduler } = require('../src/scheduler');
const { createHandler, parseRetryAfter, buildUpstreamHeaders } = require('../src/proxy');

const silentLogger = {
  debug() {}, info() {}, warn() {}, error() {}, close() {},
};

/** 起一个服务并返回 { server, port, url, close }。 */
function listen(handler) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(handler);
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      resolve({
        server,
        port,
        url: `http://127.0.0.1:${port}`,
        // Node 18 的 server.close() 不会断开空闲的 keep-alive 连接（19+ 才会），
        // 闸门到上游的连接池会让 close 一直等下去，这里手动断开
        close: () => new Promise((done) => {
          server.close(done);
          server.closeAllConnections?.();
        }),
      });
    });
  });
}

/** 搭一套「假上游 + 闸门」，返回操作句柄。 */
async function makeStack(upstreamHandler, cfgOverrides = {}) {
  const upstream = await listen(upstreamHandler);
  const cfg = {
    host: '127.0.0.1',
    port: 0,
    upstream: upstream.url,
    rpm: 1000,
    windowMs: 60000,
    limits: [],
    effectiveLimits: [{ windowMs: 60000, max: 1000 }],
    maxConcurrent: 10,
    minGapMs: 0,
    maxQueue: 100,
    queueTimeoutMs: 5000,
    penaltyMs: 50,
    maxPenaltyMs: 500,
    bypassPaths: ['/v1/messages/count_tokens'],
    logFile: '',
    logLevel: 'silent',
    ...cfgOverrides,
  };
  const scheduler = new Scheduler(cfg, { logger: silentLogger });
  const gate = await listen(createHandler(cfg, scheduler, silentLogger));
  return {
    cfg,
    scheduler,
    gateUrl: gate.url,
    async close() {
      scheduler.close();
      await gate.close();
      await upstream.close();
    },
  };
}

/** 简单的 HTTP 请求封装，收集完整响应体。 */
function request(url, { method = 'GET', headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const req = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname + target.search,
      method,
      headers,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks).toString('utf8'),
      }));
      res.on('error', reject);
    });
    req.on('error', reject);
    if (body !== null) req.write(body);
    req.end();
  });
}

/** 发请求并按到达时间记录每个 SSE 分块。 */
function requestChunks(url, { method = 'POST', headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const t0 = Date.now();
    const req = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname + target.search,
      method,
      headers,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push({ text: c.toString('utf8'), at: Date.now() - t0 }));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, chunks }));
      res.on('error', reject);
    });
    req.on('error', reject);
    if (body !== null) req.write(body);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('透传：method / path / 查询串 / 头部 / 请求体 / 状态码', async () => {
  let seen = null;
  const stack = await makeStack((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen = {
        method: req.method,
        url: req.url,
        auth: req.headers['x-api-key'],
        ua: req.headers['user-agent'],
        body: Buffer.concat(chunks).toString('utf8'),
        hasConnection: Object.keys(req.headers).includes('proxy-authorization'),
      };
      res.writeHead(201, { 'content-type': 'application/json', 'x-upstream-flag': 'yes' });
      res.end('{"ok":true}');
    });
  });

  try {
    const res = await request(`${stack.gateUrl}/v1/messages?beta=true`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': 'sk-test-123',
        'user-agent': 'claude-cli/test',
        'proxy-authorization': 'should-be-stripped',
      },
      body: '{"model":"claude-opus-5","messages":[]}',
    });

    assert.equal(res.status, 201);
    assert.equal(res.body, '{"ok":true}');
    assert.equal(res.headers['x-upstream-flag'], 'yes');
    assert.equal(seen.method, 'POST');
    assert.equal(seen.url, '/v1/messages?beta=true', '路径和查询串原样带过去');
    assert.equal(seen.auth, 'sk-test-123', '鉴权头透传，闸门不碰');
    assert.equal(seen.ua, 'claude-cli/test');
    assert.equal(seen.body, '{"model":"claude-opus-5","messages":[]}');
    assert.equal(seen.hasConnection, false, '逐跳头部被过滤');
  } finally {
    await stack.close();
  }
});

test('SSE 流式：分块按原样陆续到达，不被攒成一坨', async () => {
  const stack = await makeStack(async (req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    for (let i = 1; i <= 3; i += 1) {
      res.write(`event: delta\ndata: {"i":${i}}\n\n`);
      await sleep(60);
    }
    res.end();
  });

  try {
    const res = await requestChunks(`${stack.gateUrl}/v1/messages`, {
      headers: { 'content-type': 'application/json' },
      body: '{"stream":true}',
    });

    assert.equal(res.status, 200);
    assert.equal(res.headers['x-accel-buffering'], 'no', '显式禁用中间缓冲');
    assert.ok(res.chunks.length >= 3, `应收到至少 3 个分块，实际 ${res.chunks.length}`);
    const text = res.chunks.map((c) => c.text).join('');
    assert.ok(text.includes('{"i":1}') && text.includes('{"i":3}'));
    const spread = res.chunks[res.chunks.length - 1].at - res.chunks[0].at;
    assert.ok(spread >= 50, `分块到达应分散在时间上，实际跨度 ${spread}ms`);
  } finally {
    await stack.close();
  }
});

test('旁路：count_tokens 和 GET 在槽位占满时也能立刻返回', async () => {
  let release;
  const blocker = new Promise((r) => { release = r; });
  const stack = await makeStack(async (req, res) => {
    if (req.url === '/v1/messages') await blocker; // 占住唯一的并发名额
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ path: req.url }));
  }, { maxConcurrent: 1 });

  try {
    const hanging = request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    await sleep(80);
    assert.equal(stack.scheduler.inFlight, 1, '名额已被长请求占住');

    const bypassed = await request(`${stack.gateUrl}/v1/messages/count_tokens`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    assert.equal(bypassed.status, 200, 'count_tokens 不排队');
    assert.equal(JSON.parse(bypassed.body).path, '/v1/messages/count_tokens');

    const getRes = await request(`${stack.gateUrl}/v1/models`);
    assert.equal(getRes.status, 200, '非 POST 不排队');

    assert.equal(stack.scheduler.snapshot().stats.admitted, 1, '旁路请求不计入限流窗口');

    release();
    assert.equal((await hanging).status, 200);
  } finally {
    release();
    await stack.close();
  }
});

test('上游 429：透传给客户端，并触发全局冷却', async () => {
  const stack = await makeStack((req, res) => {
    res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '1' });
    res.end('{"type":"error","error":{"type":"rate_limit_error"}}');
  }, { penaltyMs: 50, maxPenaltyMs: 300 });

  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    assert.equal(res.status, 429, '状态码原样回给 Claude Code，让它自己退避');
    assert.equal(res.headers['retry-after'], '1');
    assert.ok(res.body.includes('rate_limit_error'), '上游错误体不被改写');

    await sleep(30);
    const snap = stack.scheduler.snapshot();
    assert.equal(snap.stats.penalties, 1, '触发一次冷却');
    assert.ok(snap.penaltyMsLeft > 0, `冷却应在生效中，实际剩余 ${snap.penaltyMsLeft}ms`);
  } finally {
    await stack.close();
  }
});

test('上游 529：同样触发冷却', async () => {
  const stack = await makeStack((req, res) => {
    res.writeHead(529, { 'content-type': 'application/json' });
    res.end('{"type":"error","error":{"type":"overloaded_error"}}');
  }, { penaltyMs: 40, maxPenaltyMs: 200 });

  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    });
    assert.equal(res.status, 529);
    await sleep(30);
    assert.equal(stack.scheduler.snapshot().stats.penalties, 1);
  } finally {
    await stack.close();
  }
});

test('上游 503：原样透传但不进冷却（只有 429/529 算限流）', async () => {
  const stack = await makeStack((req, res) => {
    res.writeHead(503, { 'content-type': 'application/json' });
    res.end('{"type":"error","error":{"type":"api_error"}}');
  }, { penaltyMs: 40, maxPenaltyMs: 200 });

  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    });
    assert.equal(res.status, 503, '上游 5xx 原样回给客户端');
    assert.ok(res.body.includes('api_error'), '上游错误体不被改写');

    await sleep(30);
    const snap = stack.scheduler.snapshot();
    assert.equal(snap.stats.admitted, 1, '这个 503 来自上游，不是闸门自己挡的');
    assert.equal(snap.stats.rejected, 0);
    assert.equal(snap.stats.penalties, 0, '非 429/529 不触发冷却');
    assert.equal(snap.penaltyMsLeft, 0, '后续请求不该被罚等');
  } finally {
    await stack.close();
  }
});

test('排队生效：maxConcurrent=1 时上游同时只看到 1 个请求', async () => {
  let active = 0;
  let peak = 0;
  const stack = await makeStack(async (req, res) => {
    active += 1;
    peak = Math.max(peak, active);
    await sleep(40);
    active -= 1;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"ok":true}');
  }, { maxConcurrent: 1 });

  try {
    const all = await Promise.all([1, 2, 3, 4].map(() => request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    })));
    assert.deepEqual(all.map((r) => r.status), [200, 200, 200, 200]);
    assert.equal(peak, 1, `上游并发峰值应为 1，实际 ${peak}`);
    assert.equal(stack.scheduler.snapshot().stats.admitted, 4);
  } finally {
    await stack.close();
  }
});

test('每 X 毫秒 Y 次：跨请求节流真的把请求摊开', async () => {
  const arrivals = [];
  const t0 = Date.now();
  const stack = await makeStack((req, res) => {
    arrivals.push(Date.now() - t0);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"ok":true}');
  }, {
    effectiveLimits: [{ windowMs: 200, max: 2 }],
    maxConcurrent: 5,
  });

  try {
    await Promise.all([1, 2, 3, 4].map(() => request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    })));
    assert.equal(arrivals.length, 4);
    const gap = arrivals[2] - arrivals[0];
    assert.ok(gap >= 190, `第 3 个请求应等窗口滚出（≥190ms），实际 ${gap}ms`);
  } finally {
    await stack.close();
  }
});

test('队列已满：返回 503 和 Anthropic 风格错误体', async () => {
  let release;
  const blocker = new Promise((r) => { release = r; });
  const stack = await makeStack(async (req, res) => {
    await blocker;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"ok":true}');
  }, { maxConcurrent: 1, maxQueue: 1 });

  try {
    const a = request(`${stack.gateUrl}/v1/messages`, { method: 'POST', body: '{}' });
    await sleep(50);
    const b = request(`${stack.gateUrl}/v1/messages`, { method: 'POST', body: '{}' });
    await sleep(50);
    const c = await request(`${stack.gateUrl}/v1/messages`, { method: 'POST', body: '{}' });

    assert.equal(c.status, 503);
    assert.equal(c.headers['retry-after'], '5');
    const payload = JSON.parse(c.body);
    assert.equal(payload.type, 'error');
    assert.equal(payload.error.type, 'overloaded_error');
    assert.ok(payload.error.message.includes('排队已满'));

    release();
    await a;
    await b;
  } finally {
    release();
    await stack.close();
  }
});

test('上游连不上：返回 502 而不是挂死', async () => {
  const dead = await listen((req, res) => res.end());
  const port = dead.port;
  await dead.close();

  const stack = await makeStack((req, res) => res.end(), { upstream: `http://127.0.0.1:${port}` });
  try {
    const res = await request(`${stack.gateUrl}/v1/messages`, { method: 'POST', body: '{}' });
    assert.equal(res.status, 502);
    assert.equal(JSON.parse(res.body).error.type, 'api_error');
  } finally {
    await stack.close();
  }
});

test('状态接口：返回限流配置与实时用量', async () => {
  const stack = await makeStack((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"ok":true}');
  }, { effectiveLimits: [{ windowMs: 1000, max: 5 }, { windowMs: 60000, max: 20 }] });

  try {
    await request(`${stack.gateUrl}/v1/messages`, { method: 'POST', body: '{}' });
    const res = await request(`${stack.gateUrl}/__gate/status`);
    assert.equal(res.status, 200);
    const snap = JSON.parse(res.body);
    assert.equal(snap.upstream, stack.cfg.upstream);
    assert.equal(snap.inFlight, 0);
    assert.equal(snap.queued, 0);
    assert.deepEqual(snap.limits.map((l) => [l.windowMs, l.max]), [[1000, 5], [60000, 20]]);
    assert.equal(snap.limits[0].used, 1, '刚放行的 1 个请求计入窗口');
    assert.equal(snap.stats.admitted, 1);

    const health = await request(`${stack.gateUrl}/__gate/health`);
    assert.equal(JSON.parse(health.body).ok, true);
  } finally {
    await stack.close();
  }
});

test('客户端排队中断开：出队并把名额让给下一个', async () => {
  let release;
  const blocker = new Promise((r) => { release = r; });
  const stack = await makeStack(async (req, res) => {
    if (req.headers['x-hold'] === '1') await blocker;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"ok":true}');
  }, { maxConcurrent: 1 });

  try {
    const holding = request(`${stack.gateUrl}/v1/messages`, {
      method: 'POST', headers: { 'x-hold': '1' }, body: '{}',
    });
    await sleep(60);

    // 手工发一个请求然后中途掐掉
    const target = new URL(`${stack.gateUrl}/v1/messages`);
    const aborted = http.request({
      hostname: target.hostname, port: target.port, path: target.pathname, method: 'POST',
    });
    aborted.on('error', () => {}); // 主动 destroy 会抛 ECONNRESET，忽略
    aborted.end('{}');
    await sleep(60);
    assert.equal(stack.scheduler.snapshot().queued, 1, '第二个请求在排队');

    aborted.destroy();
    await sleep(80);
    assert.equal(stack.scheduler.snapshot().queued, 0, '断开后立刻出队');
    assert.equal(stack.scheduler.snapshot().stats.aborted, 1);
    assert.equal(stack.scheduler.inFlight, 1, '断开的请求不占名额');

    release();
    assert.equal((await holding).status, 200);
  } finally {
    release();
    await stack.close();
  }
});

test('parseRetryAfter：支持秒数和 HTTP 日期', () => {
  assert.equal(parseRetryAfter('2'), 2000);
  assert.equal(parseRetryAfter(['3']), 3000);
  assert.equal(parseRetryAfter('0'), 0);
  assert.ok(Number.isNaN(parseRetryAfter(undefined)));
  assert.ok(Number.isNaN(parseRetryAfter('not-a-date')));
  const future = new Date(Date.now() + 5000).toUTCString();
  const ms = parseRetryAfter(future);
  assert.ok(ms > 3000 && ms <= 5000, `HTTP 日期应解析成约 5000ms，实际 ${ms}`);
});

test('buildUpstreamHeaders：换掉 host 并剔除逐跳头', () => {
  const out = buildUpstreamHeaders({
    host: 'gate.local',
    connection: 'keep-alive',
    'transfer-encoding': 'chunked',
    'x-api-key': 'sk-1',
    'content-type': 'application/json',
  }, new URL('http://127.0.0.1:17521'));

  assert.equal(out.host, '127.0.0.1:17521');
  assert.equal(out['x-api-key'], 'sk-1');
  assert.equal(out['content-type'], 'application/json');
  assert.ok(!('connection' in out));
  assert.ok(!('transfer-encoding' in out));
});
