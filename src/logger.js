'use strict';

// 极简日志：按级别过滤，同时写 stdout 和可选的日志文件。
const fs = require('fs');
const path = require('path');

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40, silent: 100 };

function createLogger({ logLevel = 'info', logFile = '' } = {}) {
  const threshold = LEVELS[logLevel] ?? LEVELS.info;
  let stream = null;

  if (logFile && threshold < LEVELS.silent) {
    try {
      fs.mkdirSync(path.dirname(logFile), { recursive: true });
      stream = fs.createWriteStream(logFile, { flags: 'a' });
      // 日志写失败不能把闸门带崩，降级成只写 stdout
      stream.on('error', (err) => {
        process.stdout.write(`[gate] 日志文件写入失败，转为仅输出到控制台: ${err.message}\n`);
        stream = null;
      });
    } catch (err) {
      process.stdout.write(`[gate] 无法打开日志文件 ${logFile}: ${err.message}\n`);
      stream = null;
    }
  }

  function write(level, msg, extra) {
    if (LEVELS[level] < threshold) return;
    const ts = new Date().toISOString();
    const tail = extra === undefined ? '' : ` ${safeJson(extra)}`;
    const line = `${ts} [${level}] ${msg}${tail}\n`;
    process.stdout.write(line);
    if (stream) stream.write(line);
  }

  function safeJson(value) {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }

  return {
    debug: (msg, extra) => write('debug', msg, extra),
    info: (msg, extra) => write('info', msg, extra),
    warn: (msg, extra) => write('warn', msg, extra),
    error: (msg, extra) => write('error', msg, extra),
    close: () => {
      if (stream) {
        stream.end();
        stream = null;
      }
    },
  };
}

module.exports = { createLogger, LEVELS };
