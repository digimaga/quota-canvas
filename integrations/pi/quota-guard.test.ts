import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import extension, { parseWorkers } from "./quota-guard.ts";

async function withFixture(fn: (f: any) => Promise<void>) {
  const env = { ...process.env };
  const dir = mkdtempSync(join(tmpdir(), "pi-quota-test-"));
  const bin = join(dir, "gate"), output = join(dir, "output"), calls = join(dir, "calls");
  writeFileSync(bin, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_GATE_CALLS"\ncat "$TEST_GATE_OUTPUT"\n');
  chmodSync(bin, 0o755);
  process.env.HOME = dir;
  process.env.ORCA_PANE_KEY = "pane";
  process.env.ORCA_AGENT_GATE_BIN = bin;
  process.env.TEST_GATE_OUTPUT = output;
  process.env.TEST_GATE_CALLS = calls;
  process.env.PI_QUOTA_WORKERS = "provider=worker";
  delete process.env.ORCA_GATE_HOOK;
  let aborted = 0;
  const warnings: string[] = [], handlers: Record<string, Function> = {};
  extension({ on(name: string, handler: Function) { handlers[name] = handler; } } as any);
  const ctx = { model: { provider: "provider" }, hasUI: true,
    ui: { notify(message: string, level: string) { assert.equal(level, "warning"); warnings.push(message); } },
    abort() { aborted++; } };
  const f = { ctx, warnings, dir, bin, output,
    get aborted() { return aborted; },
    calls: () => { try { return readFileSync(calls, "utf8").trim().split("\n"); } catch { return []; } },
    set: (verdict: string, reason = "test reason", extra = {}) => writeFileSync(output, JSON.stringify({ worker: "worker", verdict, reason, ...extra })),
    run: () => handlers.before_provider_request({}, ctx) };
  try { await fn(f); } finally {
    for (const key of Object.keys(process.env)) if (!(key in env)) delete process.env[key];
    Object.assign(process.env, env);
    rmSync(dir, { recursive: true, force: true });
  }
}

test("parse provider mappings safely", () => {
  assert.deepEqual({ ...parseWorkers(" a = one, bad, b=two, =x, c= , d=x=y, a=last ") }, { a: "last", b: "two" });
});

test("ignores non-Orca, disabled and unmapped providers", async () => withFixture(async (f) => {
  delete process.env.ORCA_PANE_KEY; await f.run();
  process.env.ORCA_PANE_KEY = ""; await f.run();
  process.env.ORCA_PANE_KEY = "pane"; process.env.ORCA_GATE_HOOK = "off"; await f.run();
  delete process.env.ORCA_GATE_HOOK; f.ctx.model.provider = "gx10"; await f.run();
  assert.deepEqual(f.calls(), []); assert.equal(f.aborted, 0); assert.deepEqual(f.warnings, []);
}));

test("unset and empty hook both keep the guard enabled", async () => withFixture(async (f) => {
  f.set("stop"); delete process.env.ORCA_GATE_HOOK; await f.run();
  process.env.ORCA_GATE_HOOK = ""; await f.run();
  assert.equal(f.aborted, 2); assert.equal(f.calls().length, 2);
}));

test("stop aborts and warns with worker, reason and allow command", async () => withFixture(async (f) => {
  f.set("stop"); await f.run();
  assert.equal(f.aborted, 1); assert.match(f.warnings[0], /worker・test reason/);
  assert.match(f.warnings[0], /orca-agent-gate allow worker/); assert.deepEqual(f.calls(), ["verdict worker"]);
}));

test("valid verdicts remain effective without a string reason", async () => withFixture(async (f) => {
  writeFileSync(f.output, JSON.stringify({ worker: "worker", verdict: "stop" })); await f.run();
  assert.equal(f.aborted, 1); assert.match(f.warnings.at(-1), /stop（worker・）/);
  writeFileSync(f.output, JSON.stringify({ worker: "worker", verdict: "stop", reason: 42 })); await f.run();
  assert.equal(f.aborted, 2); assert.match(f.warnings.at(-1), /stop（worker・）/);
  for (const verdict of ["reserve", "go", "boost", "unknown"]) {
    writeFileSync(f.output, JSON.stringify({ worker: "worker", verdict, reason: null })); await f.run();
    assert.equal(f.aborted, 2);
    if (verdict === "reserve" || verdict === "unknown") assert.match(f.warnings.at(-1), new RegExp(`${verdict}（worker・）`));
  }
}));

test("missing or different worker is treated as unknown", async () => {
  for (const response of [
    { verdict: "go" },
    { worker: "other", verdict: "go" },
    { worker: "other", verdict: "stop", allowed: true },
    { worker: "other", verdict: "stop" },
  ]) {
    await withFixture(async (f) => {
      writeFileSync(f.output, JSON.stringify(response)); await f.run();
      assert.equal(f.aborted, 0);
      assert.equal(f.warnings.length, 1);
      assert.match(f.warnings[0], /unknown（worker・残量判定を取得できませんでした）/);
      assert.deepEqual(f.calls(), ["verdict worker"]);
    });
  }
});

test("go to stop to go follows the verdict on each request", async () => withFixture(async (f) => {
  f.set("go"); await f.run(); f.set("stop"); await f.run();
  f.set("go"); await f.run();
  assert.equal(f.aborted, 1); assert.equal(f.warnings.length, 1); assert.equal(f.calls().length, 3);
}));

test("temporary allowance continues and warns only when its state changes", async () => withFixture(async (f) => {
  f.set("stop", "test reason", { allowed: true, allow_until: "2030-01-01T00:00:00Z" });
  await f.run(); await f.run();
  assert.equal(f.aborted, 0); assert.equal(f.warnings.length, 1);
  assert.match(f.warnings[0], /一時許可中（〜2030-01-01T00:00:00Z）/);
  f.set("stop", "test reason", { allowed: true, allow_until: "2030-01-01T01:00:00Z" }); await f.run();
  assert.equal(f.warnings.length, 2);
  f.set("stop", "test reason", { allowed: false }); await f.run();
  f.set("stop"); await f.run();
  assert.equal(f.aborted, 2);
  f.set("stop", "test reason", { allowed: true }); await f.run();
  assert.equal(f.aborted, 2); assert.match(f.warnings.at(-1), /一時許可中/);
}));

test("reserve warns only when verdict changes", async () => withFixture(async (f) => {
  f.set("reserve"); await f.run(); await f.run(); await f.run();
  assert.equal(f.warnings.length, 1); f.set("go"); await f.run();
  f.set("reserve"); await f.run(); assert.equal(f.warnings.length, 2); assert.equal(f.aborted, 0);
}));

test("only in-flight calls are shared per worker", async () => withFixture(async (f) => {
  f.set("go"); await Promise.all([f.run(), f.run()]); assert.equal(f.calls().length, 1);
  await f.run(); assert.equal(f.calls().length, 2);
  process.env.PI_QUOTA_WORKERS = "provider=other";
  writeFileSync(f.output, JSON.stringify({ worker: "other", verdict: "go", reason: "ok" }));
  await f.run(); assert.deepEqual(f.calls(), ["verdict worker", "verdict worker", "verdict other"]);
}));

test("CLI failure and invalid JSON continue as unknown and warn on change only", async () => withFixture(async (f) => {
  process.env.ORCA_AGENT_GATE_BIN = join(f.dir, "missing"); await f.run(); await f.run();
  assert.equal(f.aborted, 0); assert.equal(f.warnings.length, 1); assert.match(f.warnings[0], /unknown/);
  process.env.ORCA_AGENT_GATE_BIN = f.bin; writeFileSync(f.output, "bad json"); await f.run();
  assert.equal(f.warnings.length, 1); f.set("go"); await f.run();
  writeFileSync(f.output, JSON.stringify({ verdict: "invalid", reason: "bad" })); await f.run();
  assert.equal(f.aborted, 0); assert.equal(f.warnings.length, 2);
}));

test("config mapping is used only when environment mapping is absent", async () => withFixture(async (f) => {
  const configDir = join(f.dir, ".config/orca-agent-gate"); mkdirSync(configDir, { recursive: true });
  writeFileSync(join(configDir, "config.json"), JSON.stringify({ pi_providers: { provider: "worker" } }));
  f.set("stop"); process.env.PI_QUOTA_WORKERS = ""; await f.run(); assert.equal(f.aborted, 0);
  delete process.env.PI_QUOTA_WORKERS; await f.run(); assert.equal(f.aborted, 1);
  f.ctx.model.provider = "toString"; await f.run(); assert.equal(f.calls().length, 1);
}));

test("nonzero exit and oversized output continue as unknown", async () => withFixture(async (f) => {
  writeFileSync(f.bin, "#!/bin/sh\nexit 1\n"); await f.run();
  writeFileSync(f.bin, "#!/bin/sh\nhead -c 70000 /dev/zero\n"); await f.run();
  assert.equal(f.aborted, 0); assert.equal(f.warnings.length, 1);
}));
