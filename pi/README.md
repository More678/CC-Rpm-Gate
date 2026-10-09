# Connecting pi to cc-rpm-gate

English | [简体中文](README.zh-CN.md)

Lets pi (earendil-works/pi) share one rate limit and one 429 stash-and-retry queue
with every Claude Code window.

## Why an environment variable is not enough

Claude Code reads `ANTHROPIC_BASE_URL`; pi does not.

pi passes `baseURL: model.baseUrl` explicitly when it builds the Anthropic client
(`new Anthropic({ ..., baseURL: model.baseUrl })` in `dist/bundle/chunks/*.js`), and the
constructor argument wins over the default the SDK would take from the environment. That
leaves two ways to change pi's upstream:

- edit the provider's `baseUrl` in `~/.pi/agent/models.json` (permanent, and pi's own tools may rewrite it)
- call `pi.registerProvider(name, { baseUrl, headers })` from an extension (current process only)

This extension takes the second path. Per the pi docs, registering only `baseUrl` / `headers`
keeps the provider's existing model list.

## Usage

```
connect-via-gate-pi.bat            # arguments are passed through to pi
set "GATE_PORT=17540" && connect-via-gate-pi.bat
```

Keep the quotes: `set GATE_PORT=17540 && ...` would put the space before `&&` into the variable.

Without the launcher:

```
pi -e "<project path>\cc-rpm-gate\pi\gate-extension.ts"
```

## Configure named upstreams first

Every pi provider has its own `baseUrl`, while the gate has a single default upstream. So add each
provider's real address to `upstreams` in `gate.config.json`, keyed by **pi's provider name**:

```json
"upstreams": {
  "myprovider": "https://api.example.com"
}
```

The gate then accepts paths like `/__gate/u/myprovider/v1/messages` and forwards them to that upstream.
On startup the extension asks `/__gate/status` which named upstreams exist and points the `baseUrl`
of each provider with the same name at `/__gate/u/<name>`. The upstream address therefore lives in
`gate.config.json` only, with nothing to copy between the two.

If no `upstreams` are configured, the extension says so on startup and leaves every provider alone
(pi keeps talking to its upstreams directly).

## What the extension does

| Behavior | Details |
|----------|---------|
| Rewrites `baseUrl` | Each provider that has a named upstream is pointed at `/__gate/u/<name>` |
| Adds a header | `x-gate-window: pi-<pid>`, so the gate log can tell pi windows apart |
| `/gate` command | Shows queue depth, rate-limit usage, cooldown remaining, stash count and retry counters from inside pi |

## Turn pi's own retries down

The gate already absorbs 429s (2 retries by default, configurable). Stacking pi's retries on top only
makes the same request wait back and forth between two layers. Lower `retry.*` in
`~/.pi/agent/settings.json`:

```json
"retry": { "enabled": true, "maxRetries": 3, "baseDelayMs": 2000, "maxAgentDelayMs": 60000 }
```

(These are pi's defaults. If you raised them earlier to survive 429s, for example dozens of retries
with a backoff starting above ten seconds, they become overly aggressive once the gate stashes and
retries for you.)

Keep pi's `retry.provider.maxRetries` at `0`, as the pi docs also recommend.

## Note: the .bat file must be pure ASCII

`connect-via-gate-pi.bat` contains no non-ASCII characters at all, on purpose.

cmd.exe tracks its position in a `.bat` file as a byte offset, but under `chcp 65001` it counts
characters. When the two disagree it drifts and resumes execution from the middle of a line, runs half
a line as a command, and reports `'XX' is not recognized as an internal or external command`. Where
it lands depends on the exact byte layout of the file:

- the file originally had Chinese comments: 4 to 7 split-line errors in testing
- with the comments switched to ASCII: still 3 (Chinese `echo` lines trigger it too)
- fully ASCII: 0

`connect-via-gate.bat` still contains Chinese and currently runs clean, but only because its byte layout
happens to line up; an edit could bring the problem back, so it carries the same warning. Chinese
messages are printed by the extension instead (Node writes UTF-8), which is not affected.

How to verify (do not use MSYS `cmd //c`; that wrapper interferes by itself). The second match string
is the Chinese-locale form of the same cmd.exe error:

```python
import pathlib, subprocess
OUT = pathlib.Path(r'C:\Temp\o.txt')
subprocess.run(r'"...\connect-via-gate-pi.bat" --version > "C:\Temp\o.txt" 2>&1',
               shell=True, input=b'\n')
t = OUT.read_bytes().decode('utf-8', 'replace')
print(sum(1 for l in t.splitlines() if 'not recognized' in l or '不是内部' in l))
```
