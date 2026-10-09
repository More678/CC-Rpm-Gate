'use strict';

// 配置加载测试：默认值、文件覆盖、环境变量覆盖、「每 X 秒 Y 次」各种写法、非法值报错。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { loadConfig, normalizeLimits, effectiveLimits, parseDuration, DEFAULTS } = require('../src/config');

/** 写一个临时配置文件，返回路径；测试结束自动删。 */
function tmpConfig(t, obj) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-rpm-gate-'));
  const file = path.join(dir, 'gate.config.json');
  fs.writeFileSync(file, typeof obj === 'string' ? obj : JSON.stringify(obj), 'utf8');
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return file;
}

test('默认配置：不读文件不读环境变量也能用', () => {
  const cfg = loadConfig({ file: '', env: {} });
  assert.equal(cfg.port, DEFAULTS.port);
  assert.equal(cfg.host, '127.0.0.1');
  assert.equal(cfg.upstream, 'http://127.0.0.1:17521');
  assert.equal(cfg.maxConcurrent, DEFAULTS.maxConcurrent);
  assert.ok(Array.isArray(cfg.effectiveLimits) && cfg.effectiveLimits.length >= 1);
});

test('配置文件覆盖默认值，未写的字段保持默认', (t) => {
  const file = tmpConfig(t, { port: 18888, rpm: 7, maxConcurrent: 4 });
  const cfg = loadConfig({ file, env: {} });
  assert.equal(cfg.port, 18888);
  assert.equal(cfg.rpm, 7);
  assert.equal(cfg.maxConcurrent, 4);
  assert.equal(cfg.windowMs, DEFAULTS.windowMs, '没写的字段用默认值');
});

test('环境变量优先级高于配置文件', (t) => {
  const file = tmpConfig(t, { port: 18888, rpm: 7, upstream: 'http://127.0.0.1:1111' });
  const cfg = loadConfig({
    file,
    env: { GATE_PORT: '19999', GATE_RPM: '3', GATE_UPSTREAM: 'http://127.0.0.1:17521' },
  });
  assert.equal(cfg.port, 19999);
  assert.equal(cfg.rpm, 3);
  assert.equal(cfg.upstream, 'http://127.0.0.1:17521');
});

test('upstream 归一化：去掉末尾斜杠，保留前缀路径', () => {
  assert.equal(loadConfig({ file: '', env: { GATE_UPSTREAM: 'http://127.0.0.1:17521/' } }).upstream,
    'http://127.0.0.1:17521');
  assert.equal(loadConfig({ file: '', env: { GATE_UPSTREAM: 'http://127.0.0.1:8080/api/' } }).upstream,
    'http://127.0.0.1:8080/api');
});

test('非法配置：报错而不是悄悄用错值', (t) => {
  assert.throws(() => loadConfig({ file: '', env: { GATE_PORT: '70000' } }), /port 超出范围/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_PORT: 'abc' } }), /必须是整数/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_RPM: '0' } }), /rpm 必须 ≥ 1/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_MAX_CONCURRENT: '0' } }), /maxConcurrent 必须 ≥ 1/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_MIN_GAP_MS: '-5' } }), /minGapMs 不能为负/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_UPSTREAM: 'ftp://x' } }), /只支持 http\/https/);
  assert.throws(() => loadConfig({ file: '', env: { GATE_LOG_LEVEL: 'loud' } }), /logLevel 必须是/);
  assert.throws(
    () => loadConfig({ file: '', env: { GATE_PENALTY_MS: '9000', GATE_MAX_PENALTY_MS: '1000' } }),
    /不能小于/,
  );

  const bad = tmpConfig(t, '{ this is not json }');
  assert.throws(() => loadConfig({ file: bad, env: {} }), /解析配置文件失败/);

  const arr = tmpConfig(t, '[1,2,3]');
  assert.throws(() => loadConfig({ file: arr, env: {} }), /顶层必须是 JSON 对象/);
});

test('parseDuration：裸数字按秒，支持 ms/s/m/h', () => {
  assert.equal(parseDuration('10'), 10000);
  assert.equal(parseDuration('10s'), 10000);
  assert.equal(parseDuration('500ms'), 500);
  assert.equal(parseDuration('2m'), 120000);
  assert.equal(parseDuration(' 1h '), 3600000);
  assert.throws(() => parseDuration('10x'), /时间窗写法不认识/);
  assert.throws(() => parseDuration('0ms'), /必须 ≥ 1ms/);
});

test('每 X 秒 Y 次：对象数组写法', () => {
  const limits = normalizeLimits([{ seconds: 10, max: 5 }, { windowMs: 1000, max: 2 }]);
  assert.deepEqual(limits, [{ windowMs: 10000, max: 5 }, { windowMs: 1000, max: 2 }]);
});

test('每 X 秒 Y 次：字符串简写 "5/10s, 2/1s"', () => {
  assert.deepEqual(normalizeLimits('5/10s, 2/1s'), [
    { windowMs: 10000, max: 5 },
    { windowMs: 1000, max: 2 },
  ]);
  assert.deepEqual(normalizeLimits('3/500ms'), [{ windowMs: 500, max: 3 }]);
  assert.deepEqual(normalizeLimits('30/1m'), [{ windowMs: 60000, max: 30 }]);
});

test('每 X 秒 Y 次：单个对象与空值', () => {
  assert.deepEqual(normalizeLimits({ seconds: 5, max: 1 }), [{ windowMs: 5000, max: 1 }]);
  assert.deepEqual(normalizeLimits(undefined), []);
  assert.deepEqual(normalizeLimits(null), []);
  assert.deepEqual(normalizeLimits(''), []);
  assert.deepEqual(normalizeLimits([]), []);
});

test('每 X 秒 Y 次：小数秒转毫秒', () => {
  assert.deepEqual(normalizeLimits({ seconds: 1.5, max: 2 }), [{ windowMs: 1500, max: 2 }]);
  assert.deepEqual(normalizeLimits({ seconds: 0.25, max: 1 }), [{ windowMs: 250, max: 1 }]);
});

test('每 X 秒 Y 次：非法写法报错', () => {
  assert.throws(() => normalizeLimits([{ max: 5 }]), /需要 seconds 或 windowMs/);
  assert.throws(() => normalizeLimits([{ seconds: 10 }]), /max 必须是 ≥ 1 的整数/);
  assert.throws(() => normalizeLimits([{ seconds: 10, max: 0 }]), /max 必须是 ≥ 1 的整数/);
  assert.throws(() => normalizeLimits([{ seconds: 0, max: 1 }]), /seconds 必须 > 0/);
  assert.throws(() => normalizeLimits([{ seconds: -1, max: 1 }]), /seconds 必须 > 0/);
  assert.throws(() => normalizeLimits([{ windowMs: 0, max: 1 }]), /windowMs 必须是 ≥ 1 的整数/);
  assert.throws(() => normalizeLimits('5'), /limits 写法不认识/);
  assert.throws(() => normalizeLimits('x/10s'), /次数必须是整数/);
  assert.throws(() => normalizeLimits([42]), /必须是对象/);
  assert.throws(() => normalizeLimits(42), /必须是数组、对象或字符串/);
});

test('GATE_LIMITS 环境变量按字符串简写解析', () => {
  const cfg = loadConfig({ file: '', env: { GATE_LIMITS: '4/10s, 1/1s' } });
  assert.deepEqual(cfg.limits, [{ windowMs: 10000, max: 4 }, { windowMs: 1000, max: 1 }]);
  // 主窗口 rpm/windowMs 也一起进生效表
  assert.deepEqual(cfg.effectiveLimits, [
    { windowMs: 1000, max: 1 },
    { windowMs: 10000, max: 4 },
    { windowMs: 60000, max: DEFAULTS.rpm },
  ]);
});

test('生效限流表：按窗口从短到长排序，同窗口取更严格的', () => {
  const merged = effectiveLimits({
    rpm: 20,
    windowMs: 60000,
    limits: [{ windowMs: 60000, max: 8 }, { windowMs: 1000, max: 2 }],
  });
  assert.deepEqual(merged, [{ windowMs: 1000, max: 2 }, { windowMs: 60000, max: 8 }],
    '60 秒窗口写了两次，取 8 不取 20');
});

test('配置文件里用字符串写 limits 也认', (t) => {
  const file = tmpConfig(t, { limits: '6/10s', rpm: 30, windowMs: 60000 });
  const cfg = loadConfig({ file, env: {} });
  assert.deepEqual(cfg.limits, [{ windowMs: 10000, max: 6 }]);
  assert.deepEqual(cfg.effectiveLimits, [
    { windowMs: 10000, max: 6 },
    { windowMs: 60000, max: 30 },
  ]);
});

test('未知字段被忽略，不会污染配置', (t) => {
  const file = tmpConfig(t, { rmp: 999, 宽度: 'x', rpm: 5 });
  const cfg = loadConfig({ file, env: {} });
  assert.equal(cfg.rpm, 5);
  assert.equal(cfg.rmp, undefined, '拼错的字段不保留');
  assert.equal(cfg.宽度, undefined);
});

test('logFile 为空时不解析成路径', () => {
  const cfg = loadConfig({ file: '', env: { GATE_LOG_FILE: '' } });
  assert.ok(cfg.logFile.length > 0, '空字符串环境变量视为未设置，仍用默认日志路径');
  const off = loadConfig({ file: '', env: {}, overrides: { logFile: '' } });
  assert.equal(off.logFile, '');
});
