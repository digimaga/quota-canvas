import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Verdict = "stop" | "reserve" | "go" | "boost" | "unknown";
type Result = { verdict: Verdict; reason: string; allowed?: boolean; allowUntil?: string };
const verdicts = new Set<Verdict>(["stop", "reserve", "go", "boost", "unknown"]);

export function parseWorkers(value: string): Record<string, string> {
  const workers: Record<string, string> = Object.create(null);
  for (const item of value.split(",")) {
    const parts = item.split("=");
    if (parts.length !== 2) continue;
    const [provider, worker] = parts.map((part) => part.trim());
    if (provider && worker) workers[provider] = worker;
  }
  return workers;
}

function workers(): Record<string, string> {
  if (process.env.PI_QUOTA_WORKERS !== undefined) return parseWorkers(process.env.PI_QUOTA_WORKERS);
  try {
    const providers = JSON.parse(readFileSync(join(homedir(), ".config/orca-agent-gate/config.json"), "utf8")).pi_providers;
    if (!providers || typeof providers !== "object" || Array.isArray(providers)) return Object.create(null);
    const result: Record<string, string> = Object.create(null);
    for (const [key, value] of Object.entries(providers)) {
      if (key.trim() && typeof value === "string" && value.trim()) result[key] = value.trim();
    }
    return result;
  } catch { return Object.create(null); }
}

function getVerdict(worker: string): Promise<Result> {
  const bin = process.env.ORCA_AGENT_GATE_BIN || join(homedir(), ".local/bin/orca-agent-gate");
  return new Promise((resolve) => {
    execFile(bin, ["verdict", worker], { timeout: 5000, maxBuffer: 64 * 1024 }, (error, stdout) => {
      if (!error) {
        try {
          const data = JSON.parse(stdout);
          if (data && data.worker === worker && verdicts.has(data.verdict)) {
            resolve({ verdict: data.verdict, reason: typeof data.reason === "string" ? data.reason : "", allowed: data.allowed === true,
              allowUntil: typeof data.allow_until === "string" ? data.allow_until : undefined });
            return;
          }
        } catch { /* Treat invalid output as unknown. */ }
      }
      resolve({ verdict: "unknown", reason: "残量判定を取得できませんでした" });
    });
  });
}

export default function (pi: ExtensionAPI): void {
  const pending = new Map<string, Promise<Result>>();
  const previous = new Map<string, string>();
  const notify = (ctx: ExtensionContext, message: string) => {
    if (ctx.hasUI) ctx.ui.notify(message, "warning");
    else console.warn(message);
  };
  const verdictFor = async (worker: string): Promise<Result> => {
    let request = pending.get(worker);
    if (!request) {
      request = getVerdict(worker);
      pending.set(worker, request);
    }
    try {
      return await request;
    } finally { if (pending.get(worker) === request) pending.delete(worker); }
  };

  pi.on("before_provider_request", async (_event, ctx) => {
    if (!process.env.ORCA_PANE_KEY || process.env.ORCA_GATE_HOOK === "off" || !ctx.model) return;
    const worker = workers()[ctx.model.provider];
    if (!worker) return;
    const result = await verdictFor(worker);
    const state = JSON.stringify([result.verdict, result.verdict === "stop" && result.allowed === true,
      result.verdict === "stop" && result.allowed === true ? result.allowUntil : undefined]);
    const changed = previous.get(worker) !== state;
    previous.set(worker, state);
    if (result.verdict === "stop" && result.allowed === true) {
      if (changed) notify(ctx, `残量の判定が stop（${worker}）ですが、一時許可中${result.allowUntil ? `（〜${result.allowUntil}）` : ""}のため、この要求は続行します。`);
    } else if (result.verdict === "stop") {
      notify(ctx, `残量の判定が stop（${worker}・${result.reason}）のため、この要求を送りません。会話はそのままです。回復後に送り直してください。一時的に通すには orca-agent-gate allow ${worker}`);
      ctx.abort();
    } else if (changed && (result.verdict === "reserve" || result.verdict === "unknown")) {
      notify(ctx, `残量の判定が ${result.verdict}（${worker}・${result.reason}）です。この要求は続行します。`);
    }
  });
}
