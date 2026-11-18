import { readFileSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Lease = { lease_id: string; ttl: number };
type Status = { capacity: number; in_use: number; waiting: { client: string; wait_seconds: number }[] };
type Gate = { url: string; key: string; client: string; wait: number };

export class GateError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

// Do not expose response bodies or fetch errors: either could contain credentials.
export async function gateRequest<T>(gate: Gate, path: string, body?: object, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${gate.url.replace(/\/$/, "")}${path}`, {
      method: body ? "POST" : "GET",
      headers: { Authorization: `Bearer ${gate.key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch {
    throw new GateError(0, "ゲートに接続できません");
  }
  if (!response.ok) throw new GateError(response.status, `ゲートが HTTP ${response.status} を返しました`);
  try { return await response.json() as T; }
  catch { throw new GateError(response.status, "ゲートの応答を読み取れません"); }
}

export function waitingText(status: Status, client: string): string {
  if (!Number.isSafeInteger(status?.in_use) || status.in_use < 0 || !Number.isSafeInteger(status?.capacity) || status.capacity < 0 || !Array.isArray(status.waiting) || !status.waiting.every((entry) => entry && typeof entry.client === "string")) {
    throw new GateError(200, "ゲートの状態応答が不正です");
  }
  const index = status.waiting.findIndex((entry) => entry.client === client);
  const place = index < 0 ? "順番確認中" : `${index + 1} 番目`;
  return `GX10 待ち: ${place} / 使用中 ${status.in_use}/${status.capacity}`;
}

// DSH 系の認証ファイルから GX10_DEEPSEEK_API_KEY の値だけを読む（deepseek-run と同じ順。値は表示しない）
export function keyFromCredentials(text: string): string | undefined {
  const line = text.split(/\r?\n/).find((entry) => /^\s*GX10_DEEPSEEK_API_KEY\s*:/.test(entry));
  if (line === undefined) return undefined;
  const raw = line.replace(/^[^:]*:\s*/, "");
  // 引用符で囲んだ値は閉じ引用符まで、囲まない値は行末コメント（空白 + #）の手前まで
  const quoted = raw.match(/^(["'])(.*?)\1/);
  const value = (quoted ? quoted[2] : raw.startsWith("#") ? "" : raw.replace(/\s+#.*$/, "")).trim();
  return value || undefined;
}

const firstSet = (...values: (string | undefined)[]) => values.map((value) => value?.trim()).find((value) => value);

function readConfig(): { url?: string; key?: string } {
  let url = firstSet(process.env.PI_DS_GATE_URL, process.env.DEEPSEEK_RUN_GATE_URL);
  let key = firstSet(process.env.PI_DS_GATE_KEY, process.env.DEEPSEEK_RUN_API_KEY);
  if (!url) {
    try { url = JSON.parse(readFileSync(join(homedir(), ".config/deepseek-run/config.json"), "utf8")).gate_url?.trim(); }
    catch { /* Missing or unreadable config behaves like an absent URL. */ }
  }
  if (!key) {
    try { key = readFileSync(join(homedir(), ".config/deepseek-run/api_key"), "utf8").split(/\r?\n/, 1)[0].trim(); }
    catch { /* Fall through to the DSH credentials. */ }
  }
  for (const file of [".dsh/.credentials.yaml", ".dsh-resident/.credentials.yaml"]) {
    if (key) break;
    try { key = keyFromCredentials(readFileSync(join(homedir(), file), "utf8")); }
    catch { /* Report a missing key without displaying its value. */ }
  }
  return { url, key };
}

export default function (pi: ExtensionAPI): void {
  let lease: Lease | undefined;
  let leaseValid = false;
  let pending: Promise<boolean> | undefined;
  let controller: AbortController | undefined;
  let heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
  let expiryTimer: ReturnType<typeof setTimeout> | undefined;
  let heartbeatController: AbortController | undefined;
  let generation = 0;
  let currentGate: Gate | undefined;
  let currentContext: ExtensionContext | undefined;

  const notify = (ctx: ExtensionContext, message: string) => {
    if (ctx.hasUI) ctx.ui.notify(message, "warning");
    else console.warn(message);
  };
  const clearStatus = (ctx?: ExtensionContext) => { if (ctx?.hasUI) ctx.ui.setStatus("ds-gate", undefined); };
  const fail = (ctx: ExtensionContext, reason: string) => {
    const open = process.env.PI_DS_GATE_FAIL_OPEN === "1";
    notify(ctx, `ds-gate: ${reason}${open ? "（続行します）" : "（中断します）"}`);
    if (!open) ctx.abort();
    return open;
  };
  const release = async (gate: Gate, acquired: Lease) => {
    try { await gateRequest(gate, "/gate/release", { lease_id: acquired.lease_id }, AbortSignal.timeout(5000)); }
    catch { console.warn("ds-gate: 解放できませんでした。TTL で回収されます"); }
  };
  const stopHeartbeat = () => {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    if (expiryTimer) clearTimeout(expiryTimer);
    heartbeatTimer = undefined;
    expiryTimer = undefined;
    heartbeatController?.abort();
    heartbeatController = undefined;
  };
  const cleanup = async () => {
    generation++;
    controller?.abort();
    controller = undefined;
    pending = undefined;
    stopHeartbeat();
    clearStatus(currentContext);
    const held = lease, gate = currentGate;
    lease = undefined;
    leaseValid = false;
    currentGate = undefined;
    if (held && gate) await release(gate, held);
  };
  const acquire = async (gate: Gate, ctx: ExtensionContext, signal: AbortSignal): Promise<{ held: Lease; deadline: number }> => {
    if (ctx.hasUI) ctx.ui.setStatus("ds-gate", "GX10 待ち: 順番確認中");
    let polling = false;
    let finished = false;
    const update = async () => {
      if (polling || signal.aborted) return;
      polling = true;
      try {
        const status = await gateRequest<Status>(gate, "/gate/status", undefined, signal);
        if (!finished && !signal.aborted && ctx.hasUI) ctx.ui.setStatus("ds-gate", waitingText(status, gate.client));
      } catch { /* Acquire reports the authoritative failure. */ }
      finally { polling = false; }
    };
    void update();
    const timer = setInterval(() => void update(), 5000);
    const timeoutController = new AbortController();
    let timeoutTimer: ReturnType<typeof setTimeout>;
    let remaining = gate.wait * 1000 + 15000;
    const armTimeout = () => {
      const delay = Math.min(remaining, 2147483647);
      timeoutTimer = setTimeout(() => {
        remaining -= delay;
        if (remaining > 0) armTimeout();
        else timeoutController.abort();
      }, delay);
      timeoutTimer.unref();
    };
    armTimeout();
    try {
      const result = await gateRequest<Lease>(gate, "/gate/acquire", { client: gate.client, priority: "interactive", wait_seconds: gate.wait }, AbortSignal.any([signal, timeoutController.signal]));
      if (typeof result.lease_id !== "string" || !result.lease_id || !Number.isFinite(result.ttl) || result.ttl <= 0) throw new GateError(200, "ゲートのリース応答が不正です");
      // ゲートは発行時から TTL を数える。FIFO で待った時間は含めず、受信時刻から通信の遅れ分の余裕を引く
      const ttlMs = result.ttl * 1000;
      return { held: result, deadline: Date.now() + ttlMs - Math.min(5000, ttlMs / 10) };
    } finally { clearTimeout(timeoutTimer!); finished = true; clearInterval(timer); clearStatus(ctx); }
  };
  const startHeartbeat = (gate: Gate, ctx: ExtensionContext, held: Lease, epoch: number, initialDeadline: number) => {
    const ttlMs = held.ttl * 1000;
    let deadline = initialDeadline;
    const lose = (reason: string, missing = false) => {
      if (epoch !== generation || lease !== held || !leaseValid) return;
      leaseValid = false;
      if (missing) lease = undefined;
      stopHeartbeat();
      fail(ctx, reason);
    };
    const armExpiry = () => {
      if (expiryTimer) clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => lose("リースの更新を TTL までに確認できませんでした"), Math.max(0, deadline - Date.now()));
      expiryTimer.unref();
    };
    const schedule = (delay: number) => {
      heartbeatTimer = setTimeout(() => void beat(), delay);
      heartbeatTimer.unref();
    };
    const beat = async () => {
      if (epoch !== generation || lease !== held || !leaseValid) return;
      const started = Date.now();
      const remaining = deadline - started;
      if (remaining <= 0) { lose("リースの更新を TTL までに確認できませんでした"); return; }
      heartbeatController = new AbortController();
      try {
        await gateRequest(gate, "/gate/heartbeat", { lease_id: held.lease_id }, AbortSignal.any([heartbeatController.signal, AbortSignal.timeout(Math.max(1, Math.min(5000, remaining)))]));
        if (epoch !== generation || lease !== held || !leaseValid) return;
        if (Date.now() >= deadline) { lose("リースの更新を TTL までに確認できませんでした"); return; }
        deadline = started + ttlMs;
        armExpiry();
        schedule(Math.min(30000, ttlMs / 3));
      } catch (error) {
        if (epoch !== generation || lease !== held || !leaseValid) return;
        if (error instanceof GateError && error.status === 404) { lose("リースを失いました", true); return; }
        schedule(Math.max(1, Math.min(1000, deadline - Date.now())));
      }
    };
    armExpiry();
    schedule(Math.max(0, Math.min(30000, ttlMs / 3, deadline - Date.now())));
  };

  const ensure = async (ctx: ExtensionContext, signal?: AbortSignal): Promise<boolean> => {
    if (process.env.PI_DS_GATE_DISABLE === "1") { await cleanup(); return true; }
    const providers = (process.env.PI_DS_GATE_PROVIDERS ?? "gx10").split(",").map((s) => s.trim()).filter(Boolean);
    if (!ctx.model || !providers.includes(ctx.model.provider)) { await cleanup(); return true; }
    if (signal?.aborted) return false;
    if (lease && leaseValid) return true;
    if (pending) {
      if (!signal) return await pending;
      return await new Promise<boolean>((resolve) => {
        const onAbort = () => { signal.removeEventListener("abort", onAbort); resolve(false); };
        signal.addEventListener("abort", onAbort, { once: true });
        if (signal.aborted) { onAbort(); return; }
        pending!.then((result) => { signal.removeEventListener("abort", onAbort); resolve(result); });
      });
    }
    const config = readConfig();
    if (!config.url) return fail(ctx, "URL が設定されていません");
    if (!config.key) return fail(ctx, "鍵が設定されていません");
    const rawWait = process.env.PI_DS_GATE_WAIT ?? "600";
    if (!/^\d+$/.test(rawWait) || !Number.isSafeInteger(Number(rawWait))) return fail(ctx, "PI_DS_GATE_WAIT は 0 以上の整数で指定してください");
    const gate: Gate = { url: config.url, key: config.key, client: `pi:${hostname().split(".")[0]}:${process.pid}`, wait: Number(rawWait) };
    // 更新結果が不明で保持していたリースは、同じ共有取得の中で解放してから取り直す（並行要求の二重取得を防ぐ）
    const stale = lease, staleGate = currentGate;
    lease = undefined;
    stopHeartbeat();
    currentGate = gate;
    currentContext = ctx;
    controller = new AbortController();
    const epoch = generation;
    const combined = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
    const operation = (async () => {
      try {
        if (stale && staleGate) await release(staleGate, stale);
        if (epoch !== generation || combined.aborted) return false;
        const { held } = await acquire(gate, ctx, combined);
        if (epoch !== generation || combined.aborted) { await release(gate, held); return false; }
        lease = held;
        // 取得応答の遅れで失効していないか、最初の送信の前に heartbeat で確かめて期限を決め直す
        const started = Date.now();
        const ttlMs = held.ttl * 1000;
        const margin = Math.min(5000, ttlMs / 10);
        try {
          await gateRequest(gate, "/gate/heartbeat", { lease_id: held.lease_id }, AbortSignal.any([combined, AbortSignal.timeout(Math.max(1, Math.min(5000, ttlMs - margin)))]));
        } catch (error) {
          if (epoch !== generation || combined.aborted) return false;
          if (error instanceof GateError && error.status === 404) { if (lease === held) lease = undefined; return fail(ctx, "取得したリースが失効していました"); }
          return fail(ctx, "取得したリースを確認できませんでした");
        }
        if (epoch !== generation || combined.aborted) return false;
        const deadline = started + ttlMs - margin;
        if (Date.now() >= deadline) return fail(ctx, "取得したリースの TTL が経過しました");
        leaseValid = true;
        startHeartbeat(gate, ctx, held, epoch, deadline);
        return true;
      } catch (error) {
        if (epoch !== generation || combined.aborted) return false;
        return fail(ctx, error instanceof GateError && error.status === 409 ? "待ち時間の上限に達しました" : "リースを取得できませんでした");
      }
    })();
    pending = operation;
    try { return await operation; } finally { if (pending === operation) pending = undefined; }
  };

  pi.on("before_provider_request", async (_event, ctx) => {
    const signal: AbortSignal | undefined = ctx.signal;
    if (!await ensure(ctx, signal) && !signal?.aborted && process.env.PI_DS_GATE_FAIL_OPEN !== "1") ctx.abort();
  });
  pi.on("session_before_compact", async (event, ctx) => {
    if (!await ensure(ctx, event.signal)) return { cancel: true };
  });
  pi.on("session_compact", async (event) => { if (event.reason === "manual") await cleanup(); });
  pi.on("session_compact_failed", async (event) => { if (event.reason === "manual") await cleanup(); });
  pi.on("agent_settled", async () => { await cleanup(); });
  pi.on("session_shutdown", async () => { await cleanup(); });
}
