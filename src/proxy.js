'use strict';

// 反向代理：把客户端请求原样转发到上游，中间插排队闸门。
// 关键点：请求体和响应体都用流式管道，SSE 不缓冲；排队期间客户端断开立刻出队。

const http = require('http');
const https = require('https');
const { URL } = require('url');
const { requestOverrides } = require('./retry');

// 逐跳头部，不能透传给上游（也不能回给客户端）
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'host',
]);

/** 过滤逐跳头部，并把 host 换成上游的 host。 */
function buildUpstreamHeaders(incoming, upstreamUrl) {
  const out = {};
  for (const [key, value] of Object.entries(incoming)) {
    if (HOP_BY_HOP.has(key.toLowerCase())) continue;
    out[key] = value;
  }
  out.host = upstreamUrl.host;
  return out;
}

function buildClientHeaders(incoming) {
  const out = {};
  for (const [key, value] of Object.entries(incoming)) {
    if (HOP_BY_HOP.has(key.toLowerCase())) continue;
    out[key] = value;
  }
  return out;
}

/** 解析 Retry-After：既支持秒数，也支持 HTTP 日期。 */
function parseRetryAfter(value) {
  if (!value) return NaN;
  const text = Array.isArray(value) ? value[0] : String(value);
  const secs = Number(text.trim());
  if (Number.isFinite(secs)) return Math.max(0, secs * 1000);
  const when = Date.parse(text);
  if (Number.isFinite(when)) return Math.max(0, when - Date.now());
  return NaN;
}

// 请求体上限，防止异常请求把内存吃光；1M 上下文的请求也远小于这个值
const MAX_BODY_BYTES = 256 * 1024 * 1024;
// 被拦下来的限流响应体最多缓存多少：超过就不重试，直接边读边回
const MAX_CAPTURE_BYTES = 256 * 1024;
// 命名上游的访问前缀：/__gate/u/<名字>/v1/messages
const UPSTREAM_PREFIX = '/__gate/u/';

/** 把请求体整个读进内存，重试时要重放它。 */
function readBody(req, limit = MAX_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > limit) {
        const err = new Error(`请求体超过 ${limit} 字节`);
        err.code = 'BODY_TOO_LARGE';
        req.destroy(err);
        reject(err);
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

/**
 * 解析 /__gate/u/<名字>/... 前缀，挑出该走哪个上游。
 * 没有前缀就走默认 upstream。
 */
function resolveUpstream(pathname, search, upstreams, fallback) {
  if (!pathname.startsWith(UPSTREAM_PREFIX)) {
    return { target: fallback, path: pathname + search, name: '' };
  }
  const rest = pathname.slice(UPSTREAM_PREFIX.length);
  const slash = rest.indexOf('/');
  const name = slash === -1 ? rest : rest.slice(0, slash);
  const tail = slash === -1 ? '' : rest.slice(slash);
  const target = upstreams[name];
  if (!target) return { target: null, path: '', name };
  return { target, path: (tail || '/') + search, name };
}

/** 从请求头里猜一个窗口标识，仅用于日志区分是哪个 Claude Code 窗口。 */
function windowIdOf(req) {
  const h = req.headers;
  return (
    pickHeader(h['x-gate-window']) ||
    pickHeader(h['x-session-id']) ||
    pickHeader(h['x-claude-session-id']) ||
    pickHeader(h['x-request-id']) ||
    `${req.socket.remoteAddress || '?'}:${req.socket.remotePort || '?'}`
  );
}

function pickHeader(value) {
  if (value === undefined) return '';
  return Array.isArray(value) ? value[0] : String(value);
}

function sendJson(res, status, payload) {
  if (res.headersSent) return;
  const body = Buffer.from(`${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.length),
    'cache-control': 'no-store',
  });
  res.end(body);
}

/** 把之前拦下来的上游响应原样回给客户端。 */
function sendCaptured(res, captured) {
  if (res.headersSent) {
    res.end();
    return;
  }
  const headers = { ...captured.headers };
  delete headers['content-length'];
  delete headers['Content-Length'];
  headers['content-length'] = String(captured.body.length);
  res.writeHead(captured.status, headers);
  res.end(captured.body);
}

/** 按 Anthropic 的错误体格式回错，Claude Code 能正常解析并重试。 */
function sendGateError(res, status, type, message, extraHeaders = {}) {
  if (res.headersSent) {
    res.end();
    return;
  }
  const body = Buffer.from(`${JSON.stringify({
    type: 'error',
    error: { type, message: `[cc-rpm-gate] ${message}` },
  })}\n`, 'utf8');
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.length),
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  res.end(body);
}

/**
 * 创建请求处理器。
 * @param {object} cfg loadConfig() 的结果
 * @param {import('./scheduler').Scheduler} scheduler
 * @param {object} logger
 */
function createHandler(cfg, scheduler, logger, deps = {}) {
  const bypass = new Set(cfg.bypassPaths);
  const retryHook = deps.retryHook || null;
  const upstreams = cfg.upstreams || {};

  // 每个上游一套 Agent，保持连接复用
  const agents = new Map();
  function agentFor(url) {
    const key = url.origin;
    if (!agents.has(key)) {
      const transport = url.protocol === 'https:' ? https : http;
      agents.set(key, new transport.Agent({ keepAlive: true, maxSockets: 64 }));
    }
    return agents.get(key);
  }

  /** 上游状态一出来就更新冷却，必须发生在回包之前，否则客户端可能抢先重试。 */
  function updatePenalty(status, retryAfterMs, meta) {
    // 上游限流：原样透传让客户端自己退避，同时所有窗口一起进冷却
    if (status === 429 || status === 529) {
      const waited = scheduler.penalize(retryAfterMs);
      logger.warn('上游返回限流状态', { ...meta, status, cooldownMs: waited });
    } else if (status && status < 500) {
      scheduler.onSuccess();
    }
  }

  /** 是否需要排队：只拦 POST 的 messages 类请求，其他一律直放。 */
  function needsQueue(req, pathname) {
    if (req.method !== 'POST') return false;
    if (bypass.has(pathname)) return false;
    return true;
  }

  /**
   * 真正的转发动作，返回 Promise，在响应结束或出错时 resolve。
   * @param {object} opt
   * @param {URL} opt.upstreamUrl 目标上游
   * @param {string} opt.path 转发用的 path（已含查询串）
   * @param {Buffer|null} opt.body 已缓冲的请求体；null 表示直接 pipe 原始流
   * @param {number[]} [opt.intercept] 这些状态码不回给客户端，攒成 Buffer 交给调用方决定
   */
  function forward(req, res, meta, opt) {
    const { upstreamUrl, path: fwdPath, body = null, intercept = [], onStatus = null } = opt;
    const transport = upstreamUrl.protocol === 'https:' ? https : http;
    return new Promise((resolve) => {
      let finished = false;
      const done = (info) => {
        if (finished) return;
        finished = true;
        resolve(info || {});
      };

      // 保留上游 base path（例如 upstream 带前缀时）
      const basePath = upstreamUrl.pathname.replace(/\/+$/, '');
      const headers = buildUpstreamHeaders(req.headers, upstreamUrl);
      if (body !== null) {
        // 用缓冲的请求体重发，长度按实际内容重算
        delete headers['content-length'];
        delete headers['Content-Length'];
        headers['content-length'] = String(body.length);
      }
      const options = {
        protocol: upstreamUrl.protocol,
        hostname: upstreamUrl.hostname,
        port: upstreamUrl.port || (upstreamUrl.protocol === 'https:' ? 443 : 80),
        method: req.method,
        path: basePath + fwdPath,
        headers,
        agent: agentFor(upstreamUrl),
      };

      const upReq = transport.request(options, (upRes) => {
        const status = upRes.statusCode || 502;
        const retryAfterMs = parseRetryAfter(upRes.headers['retry-after']);

        // 先更新冷却再回包：否则客户端收到 429/529 立刻重试时，冷却可能还没生效。
        // 被拦下来的状态码由调用方单独处理，这里跳过。
        if (onStatus && !intercept.includes(status)) onStatus(status, retryAfterMs);

        // 要拦下来的限流响应：不写客户端，整个收进内存，交给调用方决定重试还是回包
        if (intercept.includes(status)) {
          const parts = [];
          let size = 0;
          let overflow = false;
          upRes.on('data', (chunk) => {
            size += chunk.length;
            if (size > MAX_CAPTURE_BYTES) {
              overflow = true;
              return;
            }
            parts.push(chunk);
          });
          upRes.on('end', () => done({
            status,
            retryAfterMs,
            captured: overflow ? null : {
              status,
              headers: buildClientHeaders(upRes.headers),
              body: Buffer.concat(parts),
            },
          }));
          upRes.on('error', (err) => {
            logger.warn('上游限流响应读取中断', { ...meta, error: err.message });
            done({ status, retryAfterMs, error: err });
          });
          return;
        }

        const headers = buildClientHeaders(upRes.headers);
        // SSE 必须禁用中间缓冲，否则流式输出会攒成一坨
        if (String(upRes.headers['content-type'] || '').includes('text/event-stream')) {
          headers['cache-control'] = 'no-cache, no-transform';
          headers['x-accel-buffering'] = 'no';
        }

        if (!res.headersSent) res.writeHead(status, headers);
        if (typeof res.flushHeaders === 'function') res.flushHeaders();

        upRes.pipe(res);

        upRes.on('end', () => done({ status, retryAfterMs }));
        upRes.on('error', (err) => {
          logger.warn('上游响应中断', { ...meta, error: err.message });
          res.destroy(err);
          done({ status, error: err });
        });
      });

      upReq.on('error', (err) => {
        logger.error('转发到上游失败', { ...meta, error: err.message, upstream: cfg.upstream });
        sendGateError(res, 502, 'api_error', `无法连接上游 ${cfg.upstream}: ${err.message}`);
        done({ status: 502, error: err });
      });

      // 客户端中途断开：掐掉上游请求，别让它白跑
      const onClientClose = () => {
        if (!finished) {
          upReq.destroy();
          done({ status: 499, aborted: true });
        }
      };
      req.on('aborted', onClientClose);
      res.on('close', onClientClose);

      if (body !== null) {
        // 已缓冲：直接写出去，重试时走的也是这条路
        upReq.end(body.length > 0 ? body : undefined);
      } else {
        req.pipe(upReq);
        req.on('error', (err) => {
          logger.warn('读取客户端请求体失败', { ...meta, error: err.message });
          upReq.destroy(err);
          done({ status: 400, error: err });
        });
      }
    });
  }

  return async function handler(req, res) {
    let pathname;
    try {
      pathname = new URL(req.url, 'http://localhost').pathname;
    } catch {
      sendGateError(res, 400, 'invalid_request_error', `非法请求路径: ${req.url}`);
      return;
    }

    // 闸门自身的状态接口，不走队列
    if (pathname === '/__gate/status') {
      sendJson(res, 200, {
        upstream: cfg.upstream,
        upstreams: { ...upstreams },
        listen: `${cfg.host}:${cfg.port}`,
        ...scheduler.snapshot(),
        retry: retryHook ? retryHook.snapshot() : { enabled: false },
      });
      return;
    }
    if (pathname === '/__gate/health') {
      sendJson(res, 200, { ok: true });
      return;
    }

    let search = '';
    try {
      search = new URL(req.url, 'http://localhost').search;
    } catch { /* 上面已经解析成功过，这里不会抛 */ }

    // 命名上游：/__gate/u/<名字>/... → 走 cfg.upstreams[名字]
    const route = resolveUpstream(pathname, search, upstreams, cfg.upstream);
    if (route.target === null) {
      sendGateError(res, 404, 'invalid_request_error',
        `未配置名为 ${route.name} 的上游，请在 gate.config.json 的 upstreams 里加上`);
      return;
    }
    const targetUrl = new URL(route.target);
    // 排队/旁路判断按真实业务路径走，不带 /__gate/u/<名字> 前缀
    const realPath = route.path.split('?')[0] || '/';
    const meta = {
      window: windowIdOf(req),
      path: realPath,
      method: req.method,
      ...(route.name ? { upstream: route.name } : {}),
    };

    if (!needsQueue(req, realPath)) {
      logger.debug('直放（不排队）', meta);
      await forward(req, res, meta, { upstreamUrl: targetUrl, path: route.path });
      return;
    }

    // 要重试就得能重放请求体，所以先整个读进内存；不重试时保持原来的边读边转发
    const overrides = retryHook ? requestOverrides(req.headers) : {};
    // 配了用户钩子时，钩子可能把 maxRetries 从 0 调上去，所以也要先缓存请求体
    const canRetry = Boolean(retryHook) && retryHook.cfg.enabled
      && (retryHook.maxRetriesFor(overrides) > 0 || retryHook.hasUserHook());
    let body = null;
    if (canRetry) {
      try {
        body = await readBody(req);
      } catch (err) {
        if (err.code === 'BODY_TOO_LARGE') sendGateError(res, 413, 'request_too_large', err.message);
        else res.destroy(); // 客户端发请求体时就断了，没什么可回的
        return;
      }
    }
    const intercept = canRetry ? retryHook.cfg.statuses : [];

    let attempt = 0;
    let heldMs = 0;              // 这个请求累计被暂存了多久
    let acquireOpts = {};
    let lastCaptured = null;     // 最近一次被拦下来的上游限流响应
    let stashReleaseAt = null;   // 正在暂存时的预计放行时刻

    for (;;) {
      let slot;
      const pending = scheduler.acquire(meta, acquireOpts);
      // 排队期间客户端断开，立刻出队把名额让给别人
      const onAbortWhileQueued = () => pending.cancel();
      req.once('aborted', onAbortWhileQueued);
      res.once('close', onAbortWhileQueued);

      try {
        slot = await pending;
      } catch (err) {
        req.off('aborted', onAbortWhileQueued);
        res.off('close', onAbortWhileQueued);
        if (stashReleaseAt !== null) {
          retryHook.leave(stashReleaseAt, 0);
          stashReleaseAt = null;
        }
        if (err.code === 'CLIENT_ABORTED') {
          res.destroy();
          return;
        }
        // 暂存重试没能重新排上：把上游原来的限流响应回给客户端，比回闸门自己的错有用
        if (lastCaptured) {
          sendCaptured(res, lastCaptured);
          return;
        }
        const status = err.status || 503;
        const type = status === 504 ? 'api_error' : 'overloaded_error';
        sendGateError(res, status, type, err.message, { 'retry-after': '5' });
        return;
      }

      req.off('aborted', onAbortWhileQueued);
      res.off('close', onAbortWhileQueued);
      if (stashReleaseAt !== null) {
        retryHook.leave(stashReleaseAt, slot.waitedMs);
        stashReleaseAt = null;
      }
      if (slot.waitedMs > 1000) {
        logger.info('排队后放行', { ...meta, waitedMs: slot.waitedMs, ticket: slot.ticket });
      }

      attempt += 1;
      if (attempt > 1) retryHook.recordRetry();

      const result = await forward(req, res, { ...meta, ticket: slot.ticket, attempt }, {
        upstreamUrl: targetUrl,
        path: route.path,
        body,
        intercept,
        onStatus: (status, retryAfterMs) => updatePenalty(status, retryAfterMs, meta),
      });

      // 没被拦下来：响应已经原样回给客户端了
      if (!intercept.includes(result.status)) {
        // 冷却已经在 onStatus 里（回包之前）更新过了，这里只补重试成功的计数
        if (attempt > 1 && result.status && result.status < 500) retryHook.recordRecovered();
        slot.release();
        return;
      }

      // 被拦下来的限流响应：问钩子是重试还是透传
      if (result.captured) lastCaptured = result.captured;
      let decision;
      try {
        decision = retryHook.decide({
          status: result.status,
          attempt,
          heldMs,
          retryAfterMs: result.retryAfterMs,
          overrides,
          meta,
        });
      } catch (err) {
        // 兜底：决策出错就按透传走。这里是 async 处理函数，抛出去就是未处理的
        // rejection，会把整个闸门进程带走，名额也收不回来
        logger.error('重试决策出错，按透传处理', { ...meta, error: err && err.message });
        decision = { action: 'passthrough', reason: 'error' };
      }

      if (decision.action === 'retry' && lastCaptured) {
        // 冷却按暂存时长精确设置：这段时间里所有窗口一起等，别人别去白跑
        scheduler.penalize(result.retryAfterMs, { exactMs: decision.waitMs });
        stashReleaseAt = retryHook.enter(decision.waitMs);
        heldMs += decision.waitMs;
        logger.warn('上游限流，暂存后重试', {
          ...meta,
          status: result.status,
          mode: decision.mode,
          waitMs: decision.waitMs,
          attempt,
          stashed: retryHook.stashed,
        });
        slot.release();
        // 插队重排：冷却到点后由调度器按 minGap/限流窗口逐个放行，不会一起炸出去
        acquireOpts = {
          priority: true,
          timeoutMs: decision.waitMs + Math.max(cfg.queueTimeoutMs, 30000),
        };
        continue;
      }

      // 不重试了：把上游的限流响应原样交给客户端，让它自己退避
      const waited = scheduler.penalize(result.retryAfterMs);
      logger.warn('上游返回限流状态（透传）', {
        ...meta,
        status: result.status,
        cooldownMs: waited,
        attempt,
        reason: decision.reason,
      });
      slot.release();
      if (lastCaptured) sendCaptured(res, lastCaptured);
      else {
        sendGateError(res, result.status, 'rate_limit_error',
          `上游返回 ${result.status}，且响应体过大无法暂存`, { 'retry-after': '5' });
      }
      return;
    }
  };
}

module.exports = {
  createHandler,
  parseRetryAfter,
  buildUpstreamHeaders,
  buildClientHeaders,
  resolveUpstream,
  readBody,
  HOP_BY_HOP,
  MAX_BODY_BYTES,
  MAX_CAPTURE_BYTES,
  UPSTREAM_PREFIX,
};
