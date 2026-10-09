# pi 接入 cc-rpm-gate

让 pi（earendil-works/pi）和 Claude Code 各窗口共用同一套限流与 429 暂存重试。

## 为什么不能像 Claude Code 那样设环境变量

Claude Code 认 `ANTHROPIC_BASE_URL`，pi 不认。

pi 建 Anthropic 客户端时是显式传 `baseURL: model.baseUrl` 的
（`dist/bundle/chunks/*.js` 里 `new Anthropic({ ..., baseURL: model.baseUrl })`），
构造参数盖掉了 SDK 从环境变量取的默认值。所以改 pi 的上游地址只有两条路：

- 改 `~/.pi/agent/models.json` 里 provider 的 `baseUrl`（永久生效，会被 pi 自己的工具改写）
- 用扩展调 `pi.registerProvider(name, { baseUrl, headers })`（只对当前进程生效）

这里走第二条。按 pi 文档，只注册 `baseUrl` / `headers` 时，该 provider 原有的模型列表会保留。

## 用法

```
connect-via-gate-pi.bat            # 参数原样透传给 pi
set "GATE_PORT=17540" && connect-via-gate-pi.bat
```

引号要加上：写成 `set GATE_PORT=17540 && ...` 会把 `&&` 前的空格吃进变量。

不走脚本：

```
pi -e "<项目路径>\cc-rpm-gate\pi\gate-extension.ts"
```

## 先配好命名上游

pi 的每个 provider 各有自己的 `baseUrl`，而闸门默认只有一个上游，
所以要在 `gate.config.json` 的 `upstreams` 里按 **pi 的 provider 名字** 写上它的真实地址：

```json
"upstreams": {
  "myprovider": "https://api.example.com"
}
```

闸门随即接受 `/__gate/u/myprovider/v1/messages` 这样的路径，转发到对应上游。
扩展启动时向 `/__gate/status` 问一遍有哪些命名上游，再把同名 provider 的
`baseUrl` 指到 `/__gate/u/<名字>`，所以上游地址只有 `gate.config.json` 一份，不用两边抄。

没配 `upstreams` 时扩展会在启动时提示，并且不改任何 provider（pi 照常直连上游）。

## 扩展做了什么

| 行为 | 说明 |
|------|------|
| 改 `baseUrl` | 每个命名上游对应的 provider 指到 `/__gate/u/<名字>` |
| 加请求头 | `x-gate-window: pi-<pid>`，闸门日志里用来区分是哪个 pi 窗口 |
| `/gate` 命令 | 在 pi 里查当前排队数、限流用量、冷却剩余、暂存数、重试次数 |

## 建议把 pi 自己的重试调小

闸门已经在扛 429 了（默认最多重试 2 次，可配）。pi 再叠一层重试只会让同一个请求
在两层之间来回等。`~/.pi/agent/settings.json` 里的 `retry.*` 建议降下来：

```json
"retry": { "enabled": true, "maxRetries": 3, "baseDelayMs": 2000, "maxAgentDelayMs": 60000 }
```

（这是 pi 的默认值。如果你之前为了扛 429 把它调得很大，比如几十次重试、十几秒起步的退避，
配合闸门的暂存重试就会显得过于激进。）

pi 的 `retry.provider.maxRetries` 保持 `0`，pi 文档也建议如此。

## 注意：bat 文件必须全 ASCII

`connect-via-gate-pi.bat` 里一个非 ASCII 字符都没有，这是刻意的。

cmd.exe 解析 .bat 时按字节记偏移，但在 `chcp 65001` 下按字符数，两者对不上就会
在某处「跑偏」，之后从一行中间继续执行，于是把半行当命令跑，报
`'XX' 不是内部或外部命令`。落点取决于文件的具体字节布局：

- 本文件最初带中文注释，实测报 4~7 处拆行错误
- 把中文注释换成 ASCII 后仍剩 3 处（中文 `echo` 行也会触发）
- 全部 ASCII 后为 0 处

`connect-via-gate.bat` 目前带中文也不报错，属于字节布局碰巧对齐，改动后可能复现，
所以给它加了同样的说明。中文提示由扩展（Node 写 UTF-8）打印，不受这个问题影响。

验证方法（不要用 MSYS 的 `cmd //c`，那层包装本身会干扰）：

```python
import pathlib, subprocess
OUT = pathlib.Path(r'C:\Temp\o.txt')
subprocess.run(r'"...\connect-via-gate-pi.bat" --version > "C:\Temp\o.txt" 2>&1',
               shell=True, input=b'\n')
t = OUT.read_bytes().decode('utf-8', 'replace')
print(sum(1 for l in t.splitlines() if 'not recognized' in l or '不是内部' in l))
```
