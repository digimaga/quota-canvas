import { test } from "node:test";
import assert from "node:assert/strict";
import extension, { gateRequest, waitingText, keyFromCredentials, GateError } from "./ds-gate-lease.ts";

type Handler = (...args: any[]) => Promise<any>;
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const until = async (condition: () => boolean) => {
  const end = Date.now() + 1000;
  while (!condition()) { if (Date.now() > end) throw new Error("condition timed out"); await pause(5); }
};
const hanging = (signal?: AbortSignal) => new Promise<Response>((_resolve, reject) => {
  if (signal?.aborted) return reject(new Error("aborted"));
  signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
});
async function withFixture(fn: (f: any) => Promise<void>) {
  const oldEnv = { ...process.env }, oldFetch = globalThis.fetch;
  const calls: string[] = [], warnings: string[] = [];
  let handler: (path: string, options: RequestInit) => Promise<Response> = async (path) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 60 });
    if (path === "/gate/status") return Response.json({ capacity: 4, in_use: 2, waiting: [] });
    return Response.json({});
  };
  process.env.PI_DS_GATE_URL = "https://example.test";
  process.env.PI_DS_GATE_KEY = "dummy-key";
  process.env.PI_DS_GATE_PROVIDERS = "gx10";
  delete process.env.PI_DS_GATE_DISABLE;
  delete process.env.PI_DS_GATE_FAIL_OPEN;
  // 実行元の設定ファイルや環境変数を読まないようにする
  process.env.HOME = "/nonexistent-pi-gate-test";
  delete process.env.DEEPSEEK_RUN_GATE_URL;
  delete process.env.DEEPSEEK_RUN_API_KEY;
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    const path = new URL(String(url)).pathname; calls.push(path); return handler(path, options);
  }) as typeof fetch;
  const handlers: Record<string, Handler> = {};
  extension({ on: (name: string, callback: Handler) => { handlers[name] = callback; } } as any);
  const abort = new AbortController();
  let aborted = 0;
  const ctx = { model: { provider: "gx10" }, signal: abort.signal, hasUI: true,
    ui: { setStatus() {}, notify(message: string) { warnings.push(message); } },
    abort() { aborted++; abort.abort(); } };
  const fixture = { handlers, ctx, abort, calls, warnings, get aborted() { return aborted; },
    setHandler(fn: typeof handler) { handler = fn; } };
  try { await fn(fixture); }
  finally { await handlers.session_shutdown({}, ctx); process.env = oldEnv; globalThis.fetch = oldFetch; }
}

test("status validates integer counts and queue before formatting", () => {
  const status = { capacity: 4, in_use: 4, waiting: [{ client: "other", wait_seconds: 1 }, { client: "test", wait_seconds: 2 }] };
  assert.equal(waitingText(status, "test"), "GX10 待ち: 2 番目 / 使用中 4/4");
  assert.equal(waitingText(status, "absent"), "GX10 待ち: 順番確認中 / 使用中 4/4");
  for (const value of [1.5, -1, "2", null]) {
    assert.throws(() => waitingText({ ...status, in_use: value } as any, "test"), GateError);
    assert.throws(() => waitingText({ ...status, capacity: value } as any, "test"), GateError);
  }
  assert.throws(() => waitingText({ ...status, waiting: [null] } as any, "test"), GateError);
});

test("gate errors hide response and credentials", async () => withFixture(async (f) => {
  f.setHandler(async () => new Response("dummy-key", { status: 409 }));
  await assert.rejects(gateRequest({ url: "https://example.test", key: "dummy-key", client: "test", wait: 0 }, "/gate/acquire", {}),
    (e: unknown) => e instanceof GateError && e.status === 409 && !e.message.includes("dummy-key"));
}));

test("provider changes acquire at request time and release on transition", async () => withFixture(async (f) => {
  f.ctx.model.provider = "other";
  await f.handlers.before_provider_request({}, f.ctx);
  assert.equal(f.calls.length, 0);
  f.ctx.model.provider = "gx10";
  await Promise.all([f.handlers.before_provider_request({}, f.ctx), f.handlers.before_provider_request({}, f.ctx)]);
  assert.equal(f.calls.filter((p: string) => p === "/gate/acquire").length, 1);
  f.ctx.model.provider = "other";
  await f.handlers.before_provider_request({}, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("manual compact uses its own cancellation signal and releases at completion", async () => withFixture(async (f) => {
  const signal = new AbortController();
  assert.equal(await f.handlers.session_before_compact({ reason: "manual", signal: signal.signal }, f.ctx), undefined);
  assert.equal(f.calls.filter((p: string) => p === "/gate/acquire").length, 1);
  await f.handlers.session_compact({ reason: "manual" }, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("missing URL and failed acquire stop by default, fail-open warns", async () => {
  for (const open of [false, true]) await withFixture(async (f) => {
    process.env.HOME = "/nonexistent-pi-gate-test";
    delete process.env.PI_DS_GATE_URL;
    if (open) process.env.PI_DS_GATE_FAIL_OPEN = "1";
    await f.handlers.before_provider_request({}, f.ctx);
    assert.equal(f.aborted, open ? 0 : 1);
    assert.match(f.warnings[0], /URL/);
  });
  await withFixture(async (f) => {
    f.setHandler(async (path: string) => path === "/gate/acquire" ? new Response("{}", { status: 409 }) : Response.json({}));
    await f.handlers.before_provider_request({}, f.ctx);
    assert.equal(f.aborted, 1);
  });
});

test("Esc aborts a pending acquire, including compact", async () => {
  for (const compact of [false, true]) await withFixture(async (f) => {
    let disconnected = false;
    f.setHandler(async (path: string, options: RequestInit) => {
      if (path !== "/gate/acquire") return Response.json({ capacity: 1, in_use: 1, waiting: [] });
      try { return await hanging(options.signal as AbortSignal); } finally { disconnected = true; }
    });
    const controller = compact ? new AbortController() : f.abort;
    const promise = compact ? f.handlers.session_before_compact({ reason: "manual", signal: controller.signal }, f.ctx) : f.handlers.before_provider_request({}, f.ctx);
    await until(() => f.calls.includes("/gate/acquire"));
    controller.abort();
    const result = await promise;
    assert.equal(disconnected, true);
    if (compact) assert.deepEqual(result, { cancel: true });
    assert.equal(f.calls.includes("/gate/release"), false);
  });
});

test("404 aborts execution and next concurrent requests share reacquire", async () => withFixture(async (f) => {
  let count = 0;
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: `lease-${++count}`, ttl: 0.12 });
    if (path === "/gate/heartbeat") return new Response("{}", { status: 404 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await until(() => f.aborted === 1);
  assert.equal(count, 1);
  assert.equal(f.calls.includes("/gate/release"), false);
  f.ctx.signal = new AbortController().signal;
  await Promise.all([f.handlers.before_provider_request({}, f.ctx), f.handlers.before_provider_request({}, f.ctx)]);
  assert.equal(count, 2);
}));

test("unresponsive heartbeat is aborted and expiry stops execution", async () => withFixture(async (f) => {
  let disconnected = false, beats = 0;
  f.setHandler(async (path: string, options: RequestInit) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 0.12 });
    // 取得直後の確認は通し、その後の更新だけ応答しない
    if (path === "/gate/heartbeat" && ++beats > 1) { try { return await hanging(options.signal as AbortSignal); } finally { disconnected = true; } }
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await until(() => f.aborted === 1);
  await until(() => disconnected);
  await pause(30);
  assert.equal(f.aborted, 1);
  assert.match(f.warnings[0], /TTL/);
  await f.handlers.agent_settled({}, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("heartbeat connection failures preserve the lease for release", async () => withFixture(async (f) => {
  let beats = 0;
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 0.09 });
    if (path === "/gate/heartbeat" && ++beats === 2) throw new Error("connection failed");
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await until(() => f.aborted === 1);
  const nextController = new AbortController();
  let nextAborted = 0;
  const nextCtx = { ...f.ctx, signal: nextController.signal, abort() { nextAborted++; nextController.abort(); } };
  await f.handlers.before_provider_request({}, nextCtx);
  // 次の送信では保持していたリースを先に解放してから取り直す
  const order = f.calls.filter((p: string) => p === "/gate/acquire" || p === "/gate/release");
  assert.deepEqual(order.slice(0, 3), ["/gate/acquire", "/gate/release", "/gate/acquire"]);
  assert.equal(nextAborted, 0);
  await f.handlers.agent_settled({}, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 2);
}));

test("acquire TTL is not consumed by the FIFO wait", async () => withFixture(async (f) => {
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") { await pause(80); return Response.json({ lease_id: "lease", ttl: 60 }); }
    if (path === "/gate/heartbeat") return Response.json({ ok: true, ttl: 60 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  assert.equal(f.aborted, 0);
  assert.equal(f.warnings.length, 0);
  await f.handlers.agent_settled({}, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("missing ctx.signal still aborts when acquire fails", async () => withFixture(async (f) => {
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return new Response("{}", { status: 409 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  const ctx = { ...f.ctx, signal: undefined };
  await f.handlers.before_provider_request({}, ctx);
  assert.ok(f.aborted >= 1);
}));

test("acquire times out after wait_seconds plus 15 seconds", async () => withFixture(async (f) => {
  const original = globalThis.setTimeout;
  let seenDelay: number | undefined;
  let disconnected = false;
  process.env.PI_DS_GATE_WAIT = "3";
  globalThis.setTimeout = ((fn: (...args: any[]) => void, ms?: number, ...args: any[]) => {
    if (ms === 18000) { seenDelay = ms; return original(fn, 20, ...args); }
    return original(fn, ms, ...args);
  }) as typeof setTimeout;
  try {
    f.setHandler(async (path: string, options: RequestInit) => {
      if (path === "/gate/acquire") { try { return await hanging(options.signal as AbortSignal); } finally { disconnected = true; } }
      return Response.json({ capacity: 1, in_use: 1, waiting: [] });
    });
    await f.handlers.before_provider_request({}, f.ctx);
    assert.equal(seenDelay, 18000);
    assert.equal(disconnected, true);
    assert.equal(f.aborted, 1);
  } finally { globalThis.setTimeout = original; }
}));

test("release has a timeout", async () => withFixture(async (f) => {
  const oldTimeout = AbortSignal.timeout;
  try {
    AbortSignal.timeout = ((ms: number) => oldTimeout(ms === 5000 ? 20 : ms)) as typeof AbortSignal.timeout;
    f.setHandler(async (path: string, options: RequestInit) => {
      if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 60 });
      if (path === "/gate/release") return hanging(options.signal as AbortSignal);
      return Response.json({ capacity: 1, in_use: 1, waiting: [] });
    });
    await f.handlers.before_provider_request({}, f.ctx);
    await f.handlers.agent_settled({}, f.ctx);
  } finally { AbortSignal.timeout = oldTimeout; }
}));

test("successful heartbeat renews the lease through its original TTL", async () => withFixture(async (f) => {
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 0.15 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await pause(230);
  assert.equal(f.aborted, 0);
  assert.ok(f.calls.filter((p: string) => p === "/gate/heartbeat").length >= 2);
}));

test("fail-open warns on a lost lease and retries only at next request", async () => withFixture(async (f) => {
  process.env.PI_DS_GATE_FAIL_OPEN = "1";
  let acquisitions = 0;
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: `lease-${++acquisitions}`, ttl: 0.12 });
    if (path === "/gate/heartbeat") return new Response("{}", { status: 404 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await until(() => f.warnings.length === 1);
  assert.equal(f.aborted, 0);
  assert.equal(acquisitions, 1);
  await f.handlers.before_provider_request({}, f.ctx);
  assert.equal(acquisitions, 2);
}));

test("manual compaction failure releases and automatic compaction keeps lease for retry", async () => withFixture(async (f) => {
  const signal = new AbortController().signal;
  await f.handlers.session_before_compact({ reason: "threshold", signal }, f.ctx);
  await f.handlers.session_compact({ reason: "threshold" }, f.ctx);
  assert.equal(f.calls.includes("/gate/release"), false);
  await f.handlers.session_compact_failed({ reason: "manual" }, f.ctx);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("a second compact waiter can cancel without cancelling the shared acquire", async () => withFixture(async (f) => {
  let finish: ((r: Response) => void) | undefined;
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return await new Promise<Response>((resolve) => { finish = resolve; });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  const first = f.handlers.before_provider_request({}, f.ctx);
  await until(() => !!finish);
  const controller = new AbortController();
  const second = f.handlers.session_before_compact({ reason: "manual", signal: controller.signal }, f.ctx);
  controller.abort();
  assert.deepEqual(await second, { cancel: true });
  finish!(Response.json({ lease_id: "lease", ttl: 60 }));
  await first;
  assert.equal(f.calls.filter((p: string) => p === "/gate/acquire").length, 1);
}));

test("a later provider request respects fail-open when the initiating acquire is cancelled", async () => {
  for (const open of [false, true]) await withFixture(async (f) => {
    if (open) process.env.PI_DS_GATE_FAIL_OPEN = "1";
    f.setHandler(async (path: string, options: RequestInit) => path === "/gate/acquire"
      ? hanging(options.signal as AbortSignal)
      : Response.json({ capacity: 1, in_use: 1, waiting: [] }));
    const first = f.handlers.before_provider_request({}, f.ctx);
    await until(() => f.calls.includes("/gate/acquire"));
    const secondController = new AbortController();
    let secondAborted = 0;
    const secondCtx = { ...f.ctx, signal: secondController.signal, abort() { secondAborted++; secondController.abort(); } };
    const second = f.handlers.before_provider_request({}, secondCtx);
    f.abort.abort();
    await Promise.all([first, second]);
    assert.equal(secondAborted, open ? 0 : 1);
    assert.equal(f.calls.filter((p: string) => p === "/gate/acquire").length, 1);
  });
});

test("lease expired during a delayed acquire response is rejected before sending", async () => withFixture(async (f) => {
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") return Response.json({ lease_id: "lease", ttl: 60 });
    if (path === "/gate/heartbeat") return new Response("{}", { status: 404 });
    return Response.json({ capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  assert.ok(f.aborted >= 1);
  assert.match(f.warnings[0], /失効/);
  assert.deepEqual(f.calls.filter((p: string) => p !== "/gate/status").slice(0, 2), ["/gate/acquire", "/gate/heartbeat"]);
}));

test("concurrent requests share the release and reacquire of a stale lease", async () => withFixture(async (f) => {
  let acquisitions = 0, beats = 0;
  f.setHandler(async (path: string) => {
    if (path === "/gate/acquire") { acquisitions++; return Response.json({ lease_id: `lease-${acquisitions}`, ttl: 0.09 }); }
    if (path === "/gate/heartbeat" && ++beats === 2) throw new Error("connection failed");
    if (path === "/gate/release") { await pause(30); return Response.json({}); }
    return Response.json({ ok: true, ttl: 60, capacity: 1, in_use: 1, waiting: [] });
  });
  await f.handlers.before_provider_request({}, f.ctx);
  await until(() => f.aborted === 1);
  const next = () => { const c = new AbortController(); return { ...f.ctx, signal: c.signal, abort() { c.abort(); } }; };
  await Promise.all([f.handlers.before_provider_request({}, next()), f.handlers.before_provider_request({}, next())]);
  assert.equal(acquisitions, 2);
  assert.equal(f.calls.filter((p: string) => p === "/gate/release").length, 1);
}));

test("credentials parser reads only the GX10 key", () => {
  assert.equal(keyFromCredentials("refs:\n  OTHER: x\n  GX10_DEEPSEEK_API_KEY: \"dummy-value\"\r\n"), "dummy-value");
  assert.equal(keyFromCredentials("  GX10_DEEPSEEK_API_KEY: 'dummy'"), "dummy");
  assert.equal(keyFromCredentials("OTHER: x"), undefined);
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY:"), undefined);
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY: \"dummy\" # comment"), "dummy");
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY: dummy # comment"), "dummy");
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY: dum#my"), "dum#my");
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY: # unset"), undefined);
  assert.equal(keyFromCredentials("GX10_DEEPSEEK_API_KEY:# unset"), undefined);
});

test("blank Pi settings fall back to DEEPSEEK_RUN settings", async () => withFixture(async (f) => {
  process.env.PI_DS_GATE_URL = "   ";
  process.env.DEEPSEEK_RUN_GATE_URL = "https://fallback.example.test";
  process.env.PI_DS_GATE_KEY = " ";
  process.env.DEEPSEEK_RUN_API_KEY = "fallback-dummy-key";
  const oldFetch = globalThis.fetch;
  const seen: string[] = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    seen.push(`${new URL(String(url)).origin} ${(options.headers as Record<string, string>).Authorization}`);
    return oldFetch(url, options);
  }) as typeof fetch;
  await f.handlers.before_provider_request({}, f.ctx);
  await f.handlers.session_shutdown({}, f.ctx);
  globalThis.fetch = oldFetch;
  assert.ok(seen.length >= 3);
  assert.equal(f.calls.includes("/gate/release"), true);
  assert.ok(seen.every((entry) => entry === "https://fallback.example.test Bearer fallback-dummy-key"));
  assert.equal(f.aborted, 0);
  assert.equal(f.calls.includes("/gate/acquire"), true);
}));
