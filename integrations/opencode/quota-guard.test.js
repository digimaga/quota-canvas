import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createGuard } from './quota-guard.js';

async function fixture(t, mapping = 'opencode-go=opencode,opendesign=design') {
  const home = await mkdtemp(join(tmpdir(), 'quota-guard-test-'));
  t.after(() => rm(home, { recursive: true, force: true }));
  const executable = join(home, 'gate');
  await writeFile(executable, `#!/usr/bin/env node\nconst fs=require('node:fs'); const p=process.argv[2]; if(p!=='verdict') process.exit(2); fs.appendFileSync(${JSON.stringify(join(home, 'calls'))}, process.argv[3]+'\\n'); fs.writeFileSync(${JSON.stringify(join(home, 'env-check'))}, JSON.stringify({keyPresent: Object.hasOwn(process.env, 'OPENDESIGN_API_KEY'), marker: process.env.QUOTA_GUARD_TEST_MARKER})); const input=JSON.parse(fs.readFileSync(${JSON.stringify(join(home, 'response'))},'utf8')); if(input.fail) process.exit(1); setTimeout(()=>console.log(JSON.stringify(input)), input.delay || 0);\n`);
  await chmod(executable, 0o755);
  const env = { ORCA_PANE_KEY: 'pane', OPENCODE_QUOTA_WORKERS: mapping };
  const messages = [];
  const guard = createGuard({ home, env, executable, notify: (message) => messages.push(message) });
  const response = (data) => writeFile(join(home, 'response'), JSON.stringify(data));
  const call = (provider = 'opencode-go') => guard({ model: { providerID: provider } });
  return { home, env, messages, response, call, guard };
}

test('go → stop → go は毎回問い合わせ、stop だけ中断する', async (t) => {
  const f = await fixture(t);
  await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  await f.response({ worker: 'opencode', verdict: 'stop', reason: 'test' });
  await assert.rejects(f.call(), /stop（opencode・test）.*orca-agent-gate allow opencode/);
  await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  assert.equal((await readFile(join(f.home, 'calls'), 'utf8')).trim().split('\n').length, 3);
});

test('一時許可された stop は通し、変化した時だけ通知する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'stop', allowed: true });
  await f.call(); await f.call(); assert.equal(f.messages.length, 1);
  await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  await f.response({ worker: 'opencode', verdict: 'stop', allowed: true }); await f.call();
  assert.equal(f.messages.length, 2);
});

test('worker 不一致と不正な応答は unknown で続行する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'other', verdict: 'stop' }); await f.call();
  assert.match(f.messages[0], /unknown/);
  await f.response({ worker: 'opencode', verdict: 'stop', allowed: 'true' }); await f.call();
  assert.equal(f.messages.length, 1);
});

test('reason が文字列でなくても stop は中断する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'stop', reason: 12345, allow_until: true });
  await assert.rejects(f.call(), /stop（opencode/);
});

test('CLI 失敗は unknown で続行し、同じ状態は再通知しない', async (t) => {
  const f = await fixture(t); await f.response({ fail: true }); await f.call(); await f.call();
  assert.equal(f.messages.length, 1); assert.match(f.messages[0], /unknown/);
});

test('CLI が応答しなければ 5 秒で unknown として続行する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'go', delay: 10000 });
  const started = Date.now(); await f.call();
  assert.ok(Date.now() - started >= 4800 && Date.now() - started < 8000);
  assert.match(f.messages[0], /unknown.*タイムアウト/);
});

test('reserve の変化だけ警告する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'reserve' });
  await f.call(); await f.call(); assert.equal(f.messages.length, 1);
  await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  await f.response({ worker: 'opencode', verdict: 'reserve' }); await f.call(); assert.equal(f.messages.length, 2);
});

test('環境変数が設定ファイルより優先し、空も空の対応表になる', async (t) => {
  const f = await fixture(t, 'opencode-go=env-worker');
  await mkdir(join(f.home, '.config/orca-agent-gate'), { recursive: true });
  await writeFile(join(f.home, '.config/orca-agent-gate/config.json'), JSON.stringify({ opencode_providers: { 'opencode-go': 'config-worker' } }));
  await f.response({ worker: 'env-worker', verdict: 'stop' }); await assert.rejects(f.call(), /env-worker/);
  delete f.env.OPENCODE_QUOTA_WORKERS;
  await f.response({ worker: 'config-worker', verdict: 'stop' }); await assert.rejects(f.call(), /config-worker/);
  f.env.OPENCODE_QUOTA_WORKERS = ''; await f.call();
  assert.equal((await readFile(join(f.home, 'calls'), 'utf8')).trim().split('\n').length, 2);
});

test('プロバイダの切替を毎回反映する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  await f.response({ worker: 'design', verdict: 'stop' }); await assert.rejects(f.call('opendesign'), /design/);
});

test('verdict は API キーを除き他の環境変数を継承する', async (t) => {
  const f = await fixture(t);
  const previousKey = process.env.OPENDESIGN_API_KEY; const previousMarker = process.env.QUOTA_GUARD_TEST_MARKER;
  t.after(() => {
    if (previousKey === undefined) delete process.env.OPENDESIGN_API_KEY; else process.env.OPENDESIGN_API_KEY = previousKey;
    if (previousMarker === undefined) delete process.env.QUOTA_GUARD_TEST_MARKER; else process.env.QUOTA_GUARD_TEST_MARKER = previousMarker;
  });
  process.env.OPENDESIGN_API_KEY = 'dummy-secret'; process.env.QUOTA_GUARD_TEST_MARKER = 'inherited';
  await f.response({ worker: 'opencode', verdict: 'go' }); await f.call();
  assert.deepEqual(JSON.parse(await readFile(join(f.home, 'env-check'), 'utf8')), { keyPresent: false, marker: 'inherited' });
});

test('worker 規則を環境変数と設定ファイルに適用し、不正値は照会せず通知する', async (t) => {
  const f = await fixture(t); const dir = join(f.home, '.config/orca-agent-gate'); await mkdir(dir, { recursive: true });
  const path = join(dir, 'config.json');
  for (const invalid of ['off', '.', '..', '-worker', 'a'.repeat(41), 'bad/name', '']) {
    f.env.OPENCODE_QUOTA_WORKERS = `opencode-go=${invalid}`;
    const before = f.messages.length; await f.call(); await f.call();
    assert.equal(f.messages.length, before + 1); assert.match(f.messages.at(-1), /unknown/);
    delete f.env.OPENCODE_QUOTA_WORKERS;
    await writeFile(path, JSON.stringify({ opencode_providers: { 'opencode-go': invalid } }));
    await f.call(); assert.equal(f.messages.length, before + 1);
  }
  await assert.rejects(readFile(join(f.home, 'calls')), { code: 'ENOENT' });
  for (const valid of ['.worker', '_worker', 'a'.repeat(40)]) {
    f.env.OPENCODE_QUOTA_WORKERS = `opencode-go=${valid}`;
    await f.response({ worker: valid, verdict: 'go' }); await f.call();
  }
  assert.equal((await readFile(join(f.home, 'calls'), 'utf8')).trim().split('\n').length, 3);
});

test('設定ファイルのエラーと復旧を追跡し、通常ファイル以外も待たずに続行する', async (t) => {
  const f = await fixture(t); delete f.env.OPENCODE_QUOTA_WORKERS;
  const dir = join(f.home, '.config/orca-agent-gate'); await mkdir(dir, { recursive: true });
  const path = join(dir, 'config.json');
  await mkdir(path); await f.call(); await f.call(); assert.equal(f.messages.length, 1);
  await rm(path, { recursive: true }); await writeFile(path, '{'); await f.call(); await f.call(); assert.equal(f.messages.length, 2);
  await writeFile(path, '{}'); await f.call(); await writeFile(path, '{'); await f.call(); assert.equal(f.messages.length, 3);
  await rm(path); execFileSync('mkfifo', [path]);
  const started = Date.now(); await f.call(); await f.call();
  assert.ok(Date.now() - started < 5000); assert.equal(f.messages.length, 4);
  await assert.rejects(readFile(join(f.home, 'calls')), { code: 'ENOENT' });
});

test('無効条件と未対応プロバイダでは CLI を呼ばない', async (t) => {
  const f = await fixture(t); f.env.ORCA_PANE_KEY = ''; await f.call();
  f.env.ORCA_PANE_KEY = 'pane'; f.env.ORCA_GATE_HOOK = 'off'; await f.call();
  delete f.env.ORCA_GATE_HOOK; await f.call('other');
  await f.call('constructor');
  await assert.rejects(readFile(join(f.home, 'calls')), { code: 'ENOENT' });
});

test('同一 worker の進行中の照会だけ共有し完了後は再照会する', async (t) => {
  const f = await fixture(t); await f.response({ worker: 'opencode', verdict: 'go', delay: 100 });
  await Promise.all([f.call(), f.call(), f.call()]); await f.call();
  assert.equal((await readFile(join(f.home, 'calls'), 'utf8')).trim().split('\n').length, 2);
});

test('install の dry-run は変更せず既存ファイルを退避する', async (t) => {
  const f = await fixture(t); const dir = join(f.home, '.config/opencode/plugins'); await mkdir(dir, { recursive: true });
  const target = join(dir, 'quota-guard.js'); await writeFile(target, 'original');
  const script = new URL('./install.sh', import.meta.url).pathname;
  execFileSync(script, ['--dry-run'], { env: { ...process.env, HOME: f.home } });
  assert.equal(await readFile(target, 'utf8'), 'original');
  await assert.rejects(readFile(target + '.bak'), { code: 'ENOENT' });
  execFileSync(script, [], { env: { ...process.env, HOME: f.home } });
  assert.equal(await readFile(target + '.bak', 'utf8'), 'original');
  execFileSync(script, ['--dry-run'], { env: { ...process.env, HOME: f.home } });
  execFileSync(script, [], { env: { ...process.env, HOME: f.home } });
  await writeFile(target, 'different');
  for (const args of [[], ['--dry-run']]) {
    assert.throws(() => execFileSync(script, args, { env: { ...process.env, HOME: f.home }, stdio: 'pipe' }), /quota-guard\.js\.bak/);
  }
  assert.equal(await readFile(target, 'utf8'), 'different');
  assert.equal(await readFile(target + '.bak', 'utf8'), 'original');
});

test('設定ファイルの stat が返らなくても期限で設定なし扱いにして続行する', async (t) => {
  const f = await fixture(t); delete f.env.OPENCODE_QUOTA_WORKERS;
  const messages = [];
  const guard = createGuard({
    home: f.home, env: f.env, notify: (m) => messages.push(m), configTimeoutMs: 100,
    fs: { open: () => new Promise(() => {}) },
  });
  const started = Date.now();
  assert.equal(await guard({ model: { providerID: 'opencode-go' } }), undefined);
  assert.ok(Date.now() - started < 2000);
  assert.equal(messages.length, 1);
});

test('設定の読み込みは未完了のものを共有し、後で解決すれば次は読み直す', async (t) => {
  const f = await fixture(t); delete f.env.OPENCODE_QUOTA_WORKERS;
  let calls = 0, release;
  const guard = createGuard({
    home: f.home, env: f.env, notify: () => {}, configTimeoutMs: 50,
    fs: { open: () => { calls++; return calls === 1 ? new Promise((r) => { release = r; }) : Promise.reject(Object.assign(new Error('x'), { code: 'ENOENT' })); } },
  });
  const req = { model: { providerID: 'opencode-go' } };
  const started = Date.now();
  await Promise.all([guard(req), guard(req)]);
  assert.ok(Date.now() - started < 2000);
  assert.equal(calls, 1);
  let reads = 0, closed = false;
  const content = Buffer.from('{}');
  release({
    stat: async () => ({ isFile: () => true, size: content.length }),
    read: async (buffer, offset, length, position) => {
      reads++; const n = Math.max(0, Math.min(length, content.length - position));
      content.copy(buffer, offset, position, position + n); return { bytesRead: n };
    },
    close: async () => { closed = true; },
  });
  await new Promise((r) => setTimeout(r, 10));
  assert.ok(reads >= 2 && closed);
  await guard(req);
  assert.equal(calls, 2);
});
