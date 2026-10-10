'use strict';

// 从 git 安装（npm install -g github:...）相关：配置目录的查找规则、命令行入口。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { configDir, loadConfig, ROOT, HOME_DIR_NAME } = require('../src/config');

const BIN = path.join(ROOT, 'bin', 'cc-rpm-gate.js');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'gate-install-'));
}

/** 跑命令行入口，GATE_HOME 指到临时目录，避免碰到真实配置。 */
function runBin(args, home) {
  return spawnSync(process.execPath, [BIN, ...args], {
    encoding: 'utf8',
    timeout: 20000,
    env: { ...process.env, GATE_HOME: home },
  });
}

test('configDir：GATE_HOME 优先', () => {
  const dir = tempDir();
  try {
    assert.equal(configDir({ GATE_HOME: dir }), path.resolve(dir));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('configDir：代码目录里有 gate.config.json 时用代码目录（git clone）', () => {
  const root = tempDir();
  try {
    fs.writeFileSync(path.join(root, 'gate.config.json'), '{}');
    assert.equal(configDir({}, root), root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('configDir：代码目录里没有配置（npm 全局安装）→ ~/.cc-rpm-gate', () => {
  const root = tempDir();
  try {
    assert.equal(configDir({}, root), path.join(os.homedir(), HOME_DIR_NAME));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('loadConfig：日志和钩子路径相对配置目录', () => {
  const home = tempDir();
  try {
    fs.writeFileSync(path.join(home, 'gate.config.json'), JSON.stringify({ rpm: 7, logFile: 'logs/x.log' }));
    const cfg = loadConfig({ home, env: {} });
    assert.equal(cfg.rpm, 7, '读的是配置目录里的 gate.config.json');
    assert.equal(cfg.configDir, path.resolve(home));
    assert.equal(cfg.logFile, path.join(path.resolve(home), 'logs', 'x.log'));
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('loadConfig：opts.env 只管 GATE_* 覆盖，不改配置目录', () => {
  // 仓库里有 gate.config.json，所以默认配置目录就是仓库根目录
  const cfg = loadConfig({ env: { GATE_HOME: '/should/be/ignored', GATE_RPM: '3' } });
  assert.equal(cfg.rpm, 3, 'GATE_RPM 照样生效');
  assert.equal(cfg.configDir, ROOT);
});

test('命令行：--version / --help', () => {
  const home = tempDir();
  try {
    const v = runBin(['--version'], home);
    assert.equal(v.status, 0);
    assert.match(v.stdout, new RegExp(`^cc-rpm-gate ${require('../package.json').version.replace(/\./g, '\\.')} \\(node\\)`));
    const h = runBin(['--help'], home);
    assert.equal(h.status, 0);
    assert.match(h.stdout, /--init/);
    const bad = runBin(['--nope'], home);
    assert.equal(bad.status, 2);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('命令行：--init 写默认配置，已存在时不覆盖', () => {
  const home = path.join(tempDir(), 'nested', 'home'); // 目录还不存在
  try {
    const first = runBin(['--init'], home);
    assert.equal(first.status, 0, first.stderr);
    const file = path.join(home, 'gate.config.json');
    const written = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(written.port, 17530);
    assert.equal(written.retry.maxRetries, 2);
    assert.doesNotThrow(() => loadConfig({ home, env: {} }), '写出来的配置能被正常加载');

    fs.writeFileSync(file, JSON.stringify({ rpm: 9 }));
    const second = runBin(['--init'], home);
    assert.equal(second.status, 0);
    assert.match(second.stdout, /已经存在/);
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).rpm, 9, '没被覆盖');
  } finally {
    fs.rmSync(path.dirname(path.dirname(home)), { recursive: true, force: true });
  }
});

test('命令行：--status 连不上时退出码 1', () => {
  const home = tempDir();
  try {
    // 挑一个不太可能有人在听的端口
    fs.writeFileSync(path.join(home, 'gate.config.json'), JSON.stringify({ port: 1 }));
    const r = runBin(['--status'], home);
    assert.equal(r.status, 1);
    assert.match(r.stderr, /连不上闸门/);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('package.json：bin 指向的文件存在且带 shebang', () => {
  const pkg = require('../package.json');
  const rel = pkg.bin['cc-rpm-gate'];
  const text = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  assert.ok(text.startsWith('#!/usr/bin/env node\n') || text.startsWith('#!/usr/bin/env node\r\n'));
  for (const entry of pkg.files) {
    assert.ok(fs.existsSync(path.join(ROOT, entry)), `files 里的 ${entry} 存在`);
  }
});
