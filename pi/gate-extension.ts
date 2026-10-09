/**
 * 把 pi 的请求引到 cc-rpm-gate 上，和 Claude Code 各窗口共用同一套限流与 429 暂存重试。
 *
 * 用法（参数只对当前进程生效，不改 ~/.pi/agent 里的任何文件）：
 *     pi -e <本项目>/pi/gate-extension.ts
 * 或用 connect-via-gate-pi.bat（它会带上这个参数）。
 *
 * 为什么要写成扩展：
 *   pi 建 Anthropic 客户端时是显式传 baseURL: model.baseUrl 的，
 *   所以 ANTHROPIC_BASE_URL 这个环境变量对 pi 无效（对 Claude Code 才有效）。
 *   改 provider 的 baseUrl 只能走 models.json 或 pi.registerProvider()。
 *   这里用后者：只注册 baseUrl + headers，pi 会保留该 provider 原有的模型列表。
 *
 * 上游地址不在这里重复写一遍：闸门的 /__gate/status 会报出它配了哪些命名上游，
 * 扩展按名字把对应 provider 指到 /__gate/u/<名字>，两边只有 gate.config.json 一份配置。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const DEFAULT_GATE = "http://127.0.0.1:17530";

interface GateStatus {
  upstream: string;
  upstreams: Record<string, string>;
  queued: number;
  inFlight: number;
  limits: Array<{ windowMs: number; max: number; used: number; resetInMs: number }>;
  penaltyMsLeft: number;
  retry: {
    enabled: boolean;
    mode: string;
    maxRetries: number;
    stashed: number;
    nextReleaseInMs: number;
    stats: Record<string, number>;
  };
  stats: Record<string, number>;
}

/** 闸门地址：PI_GATE_URL 优先，其次 GATE_HOST/GATE_PORT，最后默认值。 */
function gateUrl(): string {
  const explicit = (process.env.PI_GATE_URL || "").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const host = (process.env.GATE_HOST || "127.0.0.1").trim();
  const port = (process.env.GATE_PORT || "17530").trim();
  return `http://${host}:${port}`;
}

async function fetchStatus(base: string, timeoutMs = 3000): Promise<GateStatus | null> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}/__gate/status`, { signal: ctl.signal });
    if (!res.ok) return null;
    return (await res.json()) as GateStatus;
  } catch {
    return null; // 闸门没起来就当没配，不要把 pi 启动搞挂
  } finally {
    clearTimeout(timer);
  }
}

/** 本窗口的标识，闸门日志里用来区分是哪个 pi 窗口。 */
function windowId(): string {
  return `pi-${process.pid}`;
}

function describeRetry(retry: GateStatus["retry"]): string {
  if (!retry?.enabled || retry.maxRetries < 1) return "关闭（429 直接透传）";
  const how = retry.mode === "nextMinute" ? "暂存到下一分钟 :01" : "固定冷却";
  return `最多 ${retry.maxRetries} 次，${how}`;
}

export default async function (pi: ExtensionAPI) {
  const base = gateUrl();
  const status = await fetchStatus(base);

  if (!status) {
    pi.registerCommand("gate", {
      description: "查看 cc-rpm-gate 闸门状态",
      handler: async (_arg, ctx) => {
        ctx.ui.notify(`连不上闸门 ${base}，先运行 start-gate.bat 或 start-gate-py.bat`, "error");
      },
    });
    return;
  }

  const names = Object.keys(status.upstreams || {});
  const redirected: string[] = [];

  for (const name of names) {
    // 只覆盖 baseUrl 和 headers：pi 会保留这个 provider 原本的模型列表和鉴权方式
    pi.registerProvider(name, {
      baseUrl: `${base}/__gate/u/${name}`,
      headers: { "x-gate-window": windowId() },
    });
    redirected.push(name);
  }

  pi.registerCommand("gate", {
    description: "查看 cc-rpm-gate 闸门状态",
    handler: async (_arg, ctx) => {
      const now = await fetchStatus(base);
      if (!now) {
        ctx.ui.notify(`连不上闸门 ${base}`, "error");
        return;
      }
      const limits = now.limits.map((l) => `${l.used}/${l.max} 每 ${l.windowMs}ms`).join("，");
      const lines = [
        `闸门 ${base}`,
        `排队 ${now.queued}，在飞 ${now.inFlight}`,
        `限流 ${limits}`,
        `冷却剩余 ${now.penaltyMsLeft}ms`,
        `重试 ${describeRetry(now.retry)}`,
        `暂存中 ${now.retry?.stashed ?? 0}（下次放行 ${now.retry?.nextReleaseInMs ?? 0}ms）`,
        `已放行 ${now.stats?.admitted ?? 0}，重试 ${now.retry?.stats?.retried ?? 0} 次`,
      ];
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  if (redirected.length > 0) {
    pi.on("session_start", () => {
      process.stdout.write(
        `[cc-rpm-gate] 已接入闸门 ${base}；provider: ${redirected.join(", ")}；` +
        `重试 ${describeRetry(status.retry)}\n`,
      );
    });
  } else {
    pi.on("session_start", () => {
      process.stdout.write(
        `[cc-rpm-gate] 闸门 ${base} 在线，但没配命名上游。\n` +
        `             在 gate.config.json 的 upstreams 里按 pi 的 provider 名字加上它的真实 baseUrl，例如：\n` +
        `             "upstreams": { "myprovider": "https://api.example.com" }\n`,
      );
    });
  }
}
