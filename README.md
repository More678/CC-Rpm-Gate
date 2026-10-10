# cc-rpm-gate

[![CI](https://github.com/More678/CC-Rpm-Gate/actions/workflows/ci.yml/badge.svg)](https://github.com/More678/CC-Rpm-Gate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

A local request gate. It puts the requests from all your Claude Code windows (and pi)
into one queue with one shared rate limit, and when the upstream answers 429 the gate
stashes the request and retries it itself, so the client never sees the 429.

```
Claude Code windows ┐
pi windows          ┼→ 127.0.0.1:17530 (this gate) → your upstream / local proxy → model API
other clients       ┘
```

The Node and Python implementations are functionally equivalent, share one
`gate.config.json`, and have zero third-party dependencies.

## What it does

- One queue across processes: every window shares a single FIFO queue. Max concurrency,
  requests per minute, and "at most Y every X seconds" all apply at the same time.
- 429 stash and retry: when the upstream rate-limits you, the gate waits and resends on its own.
  Two modes:
  - `cooldown`: wait a fixed number of seconds (if the upstream's `Retry-After` is longer, that wins)
  - `nextMinute`: hold until second :00 or :01 of the next minute, for upstreams whose quota
    resets on the minute boundary
- Retry hook: a small JS / Python file that changes the retry count (and wait mode) per request,
  reloaded on save.
- Desktop UI (Python): edit the config and apply it to the running gate without touching any client URL.
- During a cooldown every window waits together, so no other window races ahead into the same limit.
- SSE streaming responses are relayed as they arrive, never buffered.
- Status endpoint `/__gate/status`: queue depth, window usage, cooldown remaining,
  stash count, retry counters.

## Requirements

- Node.js 18+, or Python 3.8+ (either one)
- The launchers are `.bat` files for Windows; on other platforms start it from the command line

## Install

Pick one. Neither pulls in third-party runtime dependencies.

```bash
# Node: installs the `cc-rpm-gate` command
npm install -g github:More678/CC-Rpm-Gate

# Python: installs `cc-rpm-gate-py` (console) and `cc-rpm-gate-ui` (desktop UI)
pip install "git+https://github.com/More678/CC-Rpm-Gate.git#subdirectory=python"

# Or just clone and run from the checkout (see Quick start below)
git clone https://github.com/More678/CC-Rpm-Gate.git
```

Pin a release by appending the tag, e.g. `github:More678/CC-Rpm-Gate#v1.3.0` or
`...CC-Rpm-Gate.git@v1.3.0#subdirectory=python`.

After installing, create your config and start the gate:

```bash
cc-rpm-gate --init      # or: cc-rpm-gate-py --init
cc-rpm-gate             # or: cc-rpm-gate-py / cc-rpm-gate-ui
cc-rpm-gate --status    # or: cc-rpm-gate-py --status
```

**Where config lives.** `gate.config.json`, a relative `logFile` and the hook files are looked up in
the config directory:

1. `GATE_HOME`, if set
2. the code directory, if it contains `gate.config.json` (a git clone or release archive, so existing setups are unchanged)
3. otherwise `~/.cc-rpm-gate` (an npm / pip install, whose package directory may be read-only and is replaced on upgrade)

`--init` writes a default config there without overwriting an existing one. Both installed versions
share `~/.cc-rpm-gate`, just like a checkout shares one `gate.config.json`.

To uninstall: `npm uninstall -g cc-rpm-gate` / `pip uninstall cc-rpm-gate`. Your config in
`~/.cc-rpm-gate` is left alone.

## Quick start

From a clone (if you installed with npm / pip, use `cc-rpm-gate` / `cc-rpm-gate-py` instead):

```bash
# Node
node src/server.js          # on Windows you can also double-click start-gate.bat

# Python
cd python && python -m gate # on Windows you can also double-click start-gate-py.bat
```

Both listen on the same port (17530 by default), so run only one of them.

### Desktop UI (Python)

```bash
cd python && python -m gate --ui   # on Windows: double-click start-gate-ui.bat
```

A tkinter window (standard library, nothing to install) that runs the gate in the same process.
Edit any setting, including every `retry.*` field, and press **Apply**: it takes effect on the
running gate right away. Clients keep their `ANTHROPIC_BASE_URL` untouched because the listen
address does not change. Only a `host` / `port` change needs a restart, and the window asks first.
**Save** writes `gate.config.json`, which the Node version reads too. The lower half shows queue
depth, window usage, cooldown, stashed requests, retry counters and the gate's log, refreshed every second.

If your Python lacks tkinter (some Linux distros ship it as `python3-tk`), use `python -m gate` instead.

### Connecting Claude Code

Point `ANTHROPIC_BASE_URL` at the gate:

```bash
claude --settings '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:17530"}}'
```

On Windows, `connect-via-gate.bat` does this for you and passes its arguments through to `claude`.

### Connecting pi

pi does not read `ANTHROPIC_BASE_URL`, so an extension rewrites the provider's `baseUrl` instead.
First put the provider's real address into `upstreams` in `gate.config.json`, keyed by
pi's provider name:

```json
"upstreams": { "myprovider": "https://api.example.com" }
```

Then:

```bash
pi -e pi/gate-extension.ts  # on Windows you can also use connect-via-gate-pi.bat

# installed with npm: the extension ships inside the package
pi -e "$(npm root -g)/cc-rpm-gate/pi/gate-extension.ts"
```

Type `/gate` inside pi to see the gate's status. See [pi/README.md](pi/README.md) for details.

## Configuration

Precedence from lowest to highest: built-in defaults → `gate.config.json` → environment variables.

| Key | Env var | Default | Meaning |
|-----|---------|---------|---------|
| `port` | `GATE_PORT` | `17530` | Port the gate listens on |
| `host` | `GATE_HOST` | `127.0.0.1` | Listen on this machine only |
| `upstream` | `GATE_UPSTREAM` | `http://127.0.0.1:17521` | Default upstream |
| `upstreams` | `GATE_UPSTREAMS` | `{}` | Named upstreams, reached via `/__gate/u/<name>/...` |
| `rpm` / `windowMs` | `GATE_RPM` / `GATE_WINDOW_MS` | `20` / `60000` | Main window: at most `rpm` requests per `windowMs` |
| `limits` | `GATE_LIMITS` | `[{seconds:10,max:5}]` | Extra "at most Y every X seconds" rules; also accepts `"5/10s, 2/1s"` |
| `maxConcurrent` | `GATE_MAX_CONCURRENT` | `2` | Requests in flight at once |
| `minGapMs` | `GATE_MIN_GAP_MS` | `300` | Minimum gap between two releases from the queue |
| `maxQueue` | `GATE_MAX_QUEUE` | `200` | Queue length cap; beyond it the gate answers 503 |
| `queueTimeoutMs` | `GATE_QUEUE_TIMEOUT_MS` | `600000` | Queue wait timeout; beyond it the gate answers 504 |

### 429 retry

| Key | Env var | Default | Meaning |
|-----|---------|---------|---------|
| `retry.enabled` | `GATE_RETRY_ENABLED` | `true` | When off, 429s pass straight through to the client |
| `retry.maxRetries` | `GATE_MAX_RETRIES` | `2` | How many times the gate retries; `0` disables retrying |
| `retry.mode` | `GATE_RETRY_MODE` | `cooldown` | `cooldown` or `nextMinute` |
| `retry.cooldownMs` | `GATE_RETRY_COOLDOWN_MS` | `15000` | Wait time in `cooldown` mode |
| `retry.nextMinuteOffsetMs` | `GATE_RETRY_NEXT_MINUTE_OFFSET_MS` | `1000` | `1000` aligns to second :01, `0` to second :00 |
| `retry.maxHoldMs` | `GATE_RETRY_MAX_HOLD_MS` | `180000` | Longest a single request may stay stashed before its 429 is passed through |
| `retry.maxStashed` | `GATE_RETRY_MAX_STASHED` | `50` | How many requests may be stashed at once |
| `retry.hook` | `GATE_RETRY_HOOK` | `""` | Retry hook file; empty means use `gate.hooks.js` / `gate.hooks.py` if present (see below) |

Relative paths in `logFile` and `retry.hook` resolve against the config directory, and `GATE_HOME`
overrides where that is (see [Install](#install)).

Individual requests can override the retry settings with headers, no restart needed:

- `x-gate-max-retries: 0`: do not retry this request
- `x-gate-retry-mode: nextMinute`: use next-minute mode for this request

`retry.maxRetries` is counted per request; `retry.maxHoldMs` is a per-request budget for total
stash time. If the upstream's `Retry-After` asks for more than the remaining budget, the gate does
not sit and wait: it hands the 429 back to the client and lets the client back off.

### Retry hook: change the retry count per request

For rules that config values cannot express ("retry chat requests 5 times but give up on
`count_tokens`", "use next-minute mode for this one provider"), drop a small hook file next to
`gate.config.json`:

```bash
cp gate.hooks.example.js gate.hooks.js   # Node version
cp gate.hooks.example.py gate.hooks.py   # Python version
```

```js
// gate.hooks.js
exports.onRetry = (ctx) => {
  if (ctx.path === '/v1/messages') return { maxRetries: 5 };
  if (ctx.upstream === 'myprovider') return { maxRetries: 3, mode: 'nextMinute' };
  if (ctx.heldMs > 60000) return false;   // stop retrying, pass the 429 through
};
```

```python
# gate.hooks.py
def on_retry(ctx):
    if ctx["path"] == "/v1/messages":
        return {"maxRetries": 5}
```

The gate calls it every time the upstream answers a status in `retry.statuses`.

- `ctx` has `status`, `attempt`, `heldMs`, `retryAfterMs`, `method`, `path`, `upstream`, `window`,
  `stashed`, plus the values that would apply otherwise: `maxRetries`, `mode`, `cooldownMs`,
  `nextMinuteOffsetMs`, `respectRetryAfter`, `maxHoldMs`.
- Return nothing to leave the decision alone, `false` to pass the 429 through, or an object
  overriding any of `maxRetries`, `mode`, `cooldownMs`, `nextMinuteOffsetMs`, `respectRetryAfter`,
  `maxHoldMs`, `waitMs` (an exact wait that skips the mode and `Retry-After` math).
- Precedence, lowest to highest: `retry.*` config → `x-gate-*` request headers → hook.
- The file is reloaded when it changes, so edits apply without a restart. If a save leaves it
  broken, the gate keeps using the last version that loaded. A hook that throws or returns bad
  values is logged and ignored for that request.
- `retry.hook` (or `GATE_RETRY_HOOK`) points at a different file. A name without a `.js` / `.py`
  extension, such as `"gate.hooks"`, lets each implementation pick its own file.
- Hooks must be synchronous. `retry.enabled: false` still turns retrying off completely.
- `/__gate/status` → `retry.hook` shows whether it loaded, the last error and call counters.

The hook runs as your own code inside the gate process, with the same permissions.
`gate.hooks.js` / `gate.hooks.py` are gitignored.

## Checking status

```bash
curl http://127.0.0.1:17530/__gate/status
curl http://127.0.0.1:17530/__gate/health
python -m gate --status    # built into the Python version
```

The `retry` field carries `stashed` (currently stashed requests), `nextReleaseInMs`
(countdown to the next release), plus the `retried`, `recovered`, `exhausted` and
`passedThrough` counters.

## Tests

```bash
npm test                                           # Node
cd python && python -m unittest discover -s tests   # Python
```

## Security

The gate listens on `127.0.0.1` only and has **no authentication**. Do not change `host` to
`0.0.0.0` and expose it to your LAN or the internet: anyone who can reach the port could spend
your upstream quota. The gate itself neither reads nor stores API keys; auth headers are
forwarded to the upstream untouched.

## Notes on the Windows launchers

`.bat` files must use CRLF line endings. On top of that, when cmd.exe parses a `.bat` containing
non-ASCII characters under `chcp 65001`, it can split a line mid-way and execute the remainder as
a command, and it does not happen every time. That is why `connect-via-gate-pi.bat` is pure ASCII
and leaves the Chinese messages to the extension. Please keep both properties if you edit the launchers.

## License

[MIT](LICENSE)
