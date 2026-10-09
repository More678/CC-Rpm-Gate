# cc-rpm-gate

[![CI](https://github.com/More678/CC-Rpm-Gate/actions/workflows/ci.yml/badge.svg)](https://github.com/More678/CC-Rpm-Gate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) | 简体中文

本地请求排队闸门。把多个 Claude Code 窗口（以及 pi）的请求统一排队、限流，
上游返回 429 时由闸门自己暂存重试，客户端看不到 429。

```
Claude Code 各窗口 ┐
pi 各窗口         ┼→ 127.0.0.1:17530（本闸门）→ 你的上游 / 本地代理 → 模型 API
其他客户端        ┘
```

Node 和 Python 两套实现功能对等，共用同一份 `gate.config.json`，零第三方依赖。

## 能做什么

- 跨进程统一排队：所有窗口共用一个 FIFO 队列，并发数、每分钟请求数、「每 X 秒 Y 次」同时生效
- 429 暂存重试：上游限流时闸门自己等、自己重发，两种模式：
  - `cooldown`：固定冷却若干秒（上游 `Retry-After` 更久则听上游的）
  - `nextMinute`：暂存到下一分钟的第 00 或 01 秒，适合按整分钟重置配额的上游
- 冷却期间所有窗口一起等，不会有别的窗口抢着去撞限流
- SSE 流式响应边读边写，不缓冲
- 状态接口 `/__gate/status`：排队数、窗口用量、冷却剩余、暂存数、重试统计

## 环境要求

- Node.js 18+，或 Python 3.8+（任选其一）
- Windows 下的启动脚本是 `.bat`；其他平台直接用命令行启动

## 快速开始

```bash
# Node 版
node src/server.js          # Windows 也可以双击 start-gate.bat

# Python 版
cd python && python -m gate # Windows 也可以双击 start-gate-py.bat
```

两版监听同一端口（默认 17530），只能起一个。

### 接入 Claude Code

把 `ANTHROPIC_BASE_URL` 指到闸门即可：

```bash
claude --settings '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:17530"}}'
```

Windows 下可以直接用 `connect-via-gate.bat`，参数原样透传给 `claude`。

### 接入 pi

pi 不认 `ANTHROPIC_BASE_URL`，要用扩展改 provider 的 `baseUrl`。
先在 `gate.config.json` 的 `upstreams` 里按 pi 的 provider 名字写上它的真实地址：

```json
"upstreams": { "myprovider": "https://api.example.com" }
```

然后：

```bash
pi -e pi/gate-extension.ts  # Windows 也可以用 connect-via-gate-pi.bat
```

在 pi 里输入 `/gate` 可以查看闸门状态。详见 [pi/README.zh-CN.md](pi/README.zh-CN.md)。

## 配置

配置的优先级从低到高是：默认值 → `gate.config.json` → 环境变量。

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| `port` | `GATE_PORT` | `17530` | 闸门监听端口 |
| `host` | `GATE_HOST` | `127.0.0.1` | 只监听本机 |
| `upstream` | `GATE_UPSTREAM` | `http://127.0.0.1:17521` | 默认上游 |
| `upstreams` | `GATE_UPSTREAMS` | `{}` | 命名上游，通过 `/__gate/u/<名字>/...` 访问 |
| `rpm` / `windowMs` | `GATE_RPM` / `GATE_WINDOW_MS` | `20` / `60000` | 主窗口：每 windowMs 最多 rpm 次 |
| `limits` | `GATE_LIMITS` | `[{seconds:10,max:5}]` | 额外的「每 X 秒 Y 次」，可写 `"5/10s, 2/1s"` |
| `maxConcurrent` | `GATE_MAX_CONCURRENT` | `2` | 同时在途的请求数 |
| `minGapMs` | `GATE_MIN_GAP_MS` | `300` | 相邻两次放行的最小间隔 |
| `maxQueue` | `GATE_MAX_QUEUE` | `200` | 队列长度上限，超了返回 503 |
| `queueTimeoutMs` | `GATE_QUEUE_TIMEOUT_MS` | `600000` | 排队超时，超了返回 504 |

### 429 重试

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| `retry.enabled` | `GATE_RETRY_ENABLED` | `true` | 关掉后 429 直接透传给客户端 |
| `retry.maxRetries` | `GATE_MAX_RETRIES` | `2` | 闸门自己重试几次，`0` 表示不重试 |
| `retry.mode` | `GATE_RETRY_MODE` | `cooldown` | `cooldown` 或 `nextMinute` |
| `retry.cooldownMs` | `GATE_RETRY_COOLDOWN_MS` | `15000` | cooldown 模式的等待时长 |
| `retry.nextMinuteOffsetMs` | `GATE_RETRY_NEXT_MINUTE_OFFSET_MS` | `1000` | `1000` 对齐到第 01 秒，`0` 对齐到第 00 秒 |
| `retry.maxHoldMs` | `GATE_RETRY_MAX_HOLD_MS` | `180000` | 单个请求最多暂存多久，超出就把 429 透传 |
| `retry.maxStashed` | `GATE_RETRY_MAX_STASHED` | `50` | 同时最多暂存几个请求 |

单个请求可以用请求头覆盖重试设置，不用重启闸门：

- `x-gate-max-retries: 0`：这个请求不重试
- `x-gate-retry-mode: nextMinute`：这个请求按下一分钟模式等

`retry.maxRetries` 按请求计数；`retry.maxHoldMs` 是单个请求的总暂存预算。
上游 `Retry-After` 要求等的时间超过剩余预算时，闸门不会干等，直接把 429 还给客户端，由客户端自己退避。

## 状态查询

```bash
curl http://127.0.0.1:17530/__gate/status
curl http://127.0.0.1:17530/__gate/health
python -m gate --status    # Python 版自带
```

`retry` 字段里有 `stashed`（当前暂存数）、`nextReleaseInMs`（下一个放行倒计时），
以及 `retried`、`recovered`、`exhausted`、`passedThrough` 等计数。

## 测试

```bash
npm test                                   # Node 版
cd python && python -m unittest discover -s tests  # Python 版
```

## 安全说明

闸门默认只监听 `127.0.0.1`，**没有鉴权**。不要把 `host` 改成 `0.0.0.0` 暴露到局域网或公网，
否则任何能连上这个端口的人都能用你的上游额度。闸门本身不读取、不保存 API key，
鉴权头原样透传给上游。

## Windows 启动脚本的注意事项

`.bat` 文件必须用 CRLF 行尾。另外，cmd.exe 在 `chcp 65001` 下解析带非 ASCII 字符的 .bat 时，
可能把一行从中间断开执行，而且不是每次都出现。所以 `connect-via-gate-pi.bat` 整个文件只用 ASCII，
中文提示交给扩展打印。修改启动脚本时请保持这两点。

## License

[MIT](LICENSE)
