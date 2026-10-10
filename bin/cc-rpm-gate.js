#!/usr/bin/env node
'use strict';

// npm 装出来的命令入口：
//
//   cc-rpm-gate            启动闸门
//   cc-rpm-gate --status   查询正在跑的闸门状态
//   cc-rpm-gate --init     在配置目录里写一份默认的 gate.config.json
//   cc-rpm-gate --version
//
// 配置目录：GATE_HOME → 代码目录（git clone）→ ~/.cc-rpm-gate（npm 全局安装）。

const fs = require('fs');
const http = require('http');
const path = require('path');

const { loadConfig, configDir, DEFAULTS, DEFAULT_RETRY } = require('../src/config');

const USAGE = `cc-rpm-gate —— Claude Code / pi 本地请求排队闸门

  cc-rpm-gate              启动闸门
  cc-rpm-gate --status     查询正在跑的闸门状态
  cc-rpm-gate --init       在配置目录里写一份默认的 gate.config.json
  cc-rpm-gate --version    显示版本号
  cc-rpm-gate --help       显示本帮助

配置目录（gate.config.json、日志、钩子文件都在这里找）：
  1. 环境变量 GATE_HOME
  2. 代码目录里有 gate.config.json 时就用代码目录（git clone 的情况）
  3. 否则 ~/.cc-rpm-gate（npm 全局安装的情况）

Python 版（图形界面、--ui）见 README。
`;

/** 写一份默认配置到配置目录；已经有了就不动它。 */
function init() {
  const home = configDir();
  const target = path.join(home, 'gate.config.json');
  if (fs.existsSync(target)) {
    process.stdout.write(`[cc-rpm-gate] 配置文件已经存在，没有改动: ${target}\n`);
    return 0;
  }
  // 从默认值生成，顺序和仓库里那份 gate.config.json 对齐，方便对照改
  const cfg = {
    port: DEFAULTS.port,
    host: DEFAULTS.host,
    upstream: DEFAULTS.upstream,
    rpm: DEFAULTS.rpm,
    windowMs: DEFAULTS.windowMs,
    limits: DEFAULTS.limits,
    maxConcurrent: DEFAULTS.maxConcurrent,
    minGapMs: DEFAULTS.minGapMs,
    maxQueue: DEFAULTS.maxQueue,
    queueTimeoutMs: DEFAULTS.queueTimeoutMs,
    penaltyMs: DEFAULTS.penaltyMs,
    maxPenaltyMs: DEFAULTS.maxPenaltyMs,
    bypassPaths: DEFAULTS.bypassPaths,
    retry: { ...DEFAULT_RETRY },
    upstreams: {},
    logFile: DEFAULTS.logFile,
    logLevel: DEFAULTS.logLevel,
  };
  fs.mkdirSync(home, { recursive: true });
  fs.writeFileSync(target, `${JSON.stringify(cfg, null, 2)}\n`, 'utf8');
  process.stdout.write(`[cc-rpm-gate] 已写入默认配置: ${target}\n`);
  return 0;
}

/** 查询正在跑的闸门状态（和 Python 版 --status 对齐）。 */
function status() {
  const cfg = loadConfig();
  const url = `http://${cfg.host}:${cfg.port}/__gate/status`;
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 5000 }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        if (res.statusCode !== 200) {
          process.stderr.write(`[cc-rpm-gate] 闸门返回 ${res.statusCode}: ${body}\n`);
          resolve(1);
          return;
        }
        try {
          process.stdout.write(`${JSON.stringify(JSON.parse(body), null, 2)}\n`);
        } catch {
          process.stdout.write(`${body}\n`);
        }
        resolve(0);
      });
    });
    const fail = (err) => {
      process.stderr.write(`[cc-rpm-gate] 连不上闸门 ${url}: ${err.message}\n`);
      resolve(1);
    };
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', fail);
  });
}

async function main(argv) {
  const arg = argv[0];
  if (arg === '-h' || arg === '--help') {
    process.stdout.write(USAGE);
    return 0;
  }
  if (arg === '-v' || arg === '--version') {
    process.stdout.write(`cc-rpm-gate ${require('../package.json').version} (node)\n`);
    return 0;
  }
  if (arg === '--init') return init();
  if (arg === '--status') return status();
  if (arg !== undefined) {
    process.stderr.write(`[cc-rpm-gate] 不认识的参数: ${argv.join(' ')}\n\n${USAGE}`);
    return 2;
  }
  require('../src/server').start();
  return null; // 服务起来了，别退出
}

main(process.argv.slice(2))
  .then((code) => {
    if (code !== null) process.exit(code);
  })
  .catch((err) => {
    process.stderr.write(`[cc-rpm-gate] 启动失败: ${err.message}\n`);
    process.exit(1);
  });
