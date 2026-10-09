'use strict';

// 启动入口：加载配置 → 建调度器 → 起 HTTP 服务。
// 端口被占用时给明确提示并以 1 退出，避免多开两个闸门互相抢端口。

const http = require('http');
const { loadConfig } = require('./config');
const { createLogger } = require('./logger');
const { Scheduler } = require('./scheduler');
const { createHandler } = require('./proxy');
const { createRetryHook } = require('./retry');

function describeLimits(limits) {
  return limits.map((lim) => `${lim.max}次/${lim.windowMs}ms`).join('，');
}

/** 把 retry 配置说成一句人话，启动日志里好看。 */
function describeRetry(retry) {
  if (!retry.enabled || retry.maxRetries < 1) return '关闭（429 直接透传）';
  const how = retry.mode === 'nextMinute'
    ? `暂存到下一分钟第 ${Math.round(retry.nextMinuteOffsetMs / 1000)} 秒`
    : `冷却 ${retry.cooldownMs}ms`;
  return `最多重试 ${retry.maxRetries} 次，${how}`;
}

function start(opts = {}) {
  const cfg = loadConfig(opts.configOpts);
  const logger = createLogger(cfg);
  const scheduler = new Scheduler(cfg, { logger });
  const retryHook = createRetryHook(cfg, { logger });
  const handler = createHandler(cfg, scheduler, logger, { retryHook });

  const server = http.createServer(handler);
  // SSE 长连接不能被服务器主动掐断：超时交给上游和客户端自己管
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  server.timeout = 0;
  server.keepAliveTimeout = 75000;

  server.on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
      logger.error(`端口 ${cfg.host}:${cfg.port} 已被占用。闸门可能已经在跑了，或者换个 GATE_PORT。`);
      process.exit(1);
    }
    if (err.code === 'EACCES') {
      logger.error(`没有权限监听 ${cfg.host}:${cfg.port}，换一个大于 1024 的端口。`);
      process.exit(1);
    }
    logger.error('服务器错误', { error: err.message, code: err.code });
    process.exit(1);
  });

  server.listen(cfg.port, cfg.host, () => {
    logger.info('闸门已启动', {
      listen: `http://${cfg.host}:${cfg.port}`,
      upstream: cfg.upstream,
      upstreams: Object.keys(cfg.upstreams || {}),
      limits: describeLimits(cfg.effectiveLimits),
      maxConcurrent: cfg.maxConcurrent,
      minGapMs: cfg.minGapMs,
      retry: describeRetry(cfg.retry),
    });
    logger.info(`把 ANTHROPIC_BASE_URL 指到 http://${cfg.host}:${cfg.port} 即可接入；状态查询 /__gate/status`);
  });

  let closing = false;
  const shutdown = (signal) => {
    if (closing) return;
    closing = true;
    logger.info(`收到 ${signal}，正在关闭闸门`);
    scheduler.close();
    server.close(() => {
      logger.close();
      process.exit(0);
    });
    // 兜底：5 秒内没关干净就硬退，别卡住终端
    setTimeout(() => {
      logger.close();
      process.exit(0);
    }, 5000).unref();
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  return { server, scheduler, logger, cfg, retryHook };
}

if (require.main === module) {
  try {
    start();
  } catch (err) {
    process.stderr.write(`[cc-rpm-gate] 启动失败: ${err.message}\n`);
    process.exit(1);
  }
}

module.exports = { start };
