import { execFile } from 'node:child_process';
import { open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const namePattern = /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/;
const workerPattern = /^[A-Za-z0-9._-]{1,40}$/;
const validWorker = (worker) => typeof worker === 'string' && workerPattern.test(worker) &&
  !worker.startsWith('-') && !['off', '.', '..'].includes(worker);
const verdicts = new Set(['go', 'boost', 'reserve', 'stop', 'unknown']);

function envWorkers(value) {
  const result = Object.create(null);
  for (const entry of value.split(',')) {
    const parts = entry.split('=');
    if (parts.length !== 2) continue;
    const [provider, worker] = parts.map((part) => part.trim());
    if (namePattern.test(provider)) result[provider] = worker;
  }
  return result;
}

async function workerFor(provider, env, home, configState, fs, timeoutMs, shared) {
  if (env.OPENCODE_QUOTA_WORKERS !== undefined) return envWorkers(env.OPENCODE_QUOTA_WORKERS)[provider];
  if (!shared.waiters) {
    // 未完了の読み込みは 1 つだけ。完了ハンドラーも 1 つで、待っている要求へ配る（期限切れの要求は先に外す）
    const waiters = new Set();
    shared.waiters = waiters;
    const settle = (ok, value) => {
      if (shared.waiters === waiters) shared.waiters = null;
      for (const waiter of waiters) waiter(ok, value);
      waiters.clear();
    };
    (async () => {
      const path = join(home, '.config/orca-agent-gate/config.json');
      const handle = await fs.open(path, constants.O_RDONLY | constants.O_NONBLOCK);
      try {
        const info = await handle.stat();
        if (!info.isFile()) throw new Error('not a regular file');
        const limit = 1024 * 1024;
        if (info.size > limit) throw new Error('config too large');
        const buffer = Buffer.alloc(limit + 1);
        let length = 0;
        while (length < buffer.length) {
          const { bytesRead } = await handle.read(buffer, length, buffer.length - length, length);
          if (bytesRead === 0) break;
          length += bytesRead;
        }
        if (length > limit) throw new Error('config too large');
        return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, length)));
      } finally {
        await handle.close().catch(() => {});
      }
    })().then((value) => settle(true, value), (error) => settle(false, error));
  }
  const waiters = shared.waiters;
  let timer;
  let waiter;
  try {
    const config = await new Promise((resolve, reject) => {
      waiter = (ok, value) => { clearTimeout(timer); (ok ? resolve : reject)(value); };
      waiters.add(waiter);
      timer = setTimeout(() => {
        waiters.delete(waiter);
        reject(Object.assign(new Error('timeout'), { name: 'TimeoutError' }));
      }, timeoutMs);
    });
    configState(null);
    return Object.hasOwn(config?.opencode_providers ?? {}, provider) ? config.opencode_providers[provider] : undefined;
  } catch (error) {
    configState(error instanceof SyntaxError ? 'invalid' : (error.code || error.name));
    return undefined;
  } finally {
    clearTimeout(timer);
    waiters.delete(waiter);
  }
}

function query(executable, worker) {
  return new Promise((resolve) => {
    const { OPENDESIGN_API_KEY: _excluded, ...childEnv } = process.env;
    const child = execFile(executable, ['verdict', worker], { maxBuffer: 64 * 1024, env: childEnv }, (error, stdout) => {
      clearTimeout(timer);
      if (error) return resolve({ worker, verdict: 'unknown', reason: 'CLI の実行に失敗しました' });
      try {
        const value = JSON.parse(stdout);
        if (!value || typeof value !== 'object' || Array.isArray(value) || value.worker !== worker ||
            !verdicts.has(value.verdict) || (value.allowed !== undefined && typeof value.allowed !== 'boolean')) throw new Error('invalid verdict');
        // 補足項目（reason・allow_until）の型が不正でもその項目だけ捨て、停止の判定は保つ
        if (value.reason !== undefined && typeof value.reason !== 'string') delete value.reason;
        if (value.allow_until !== undefined && typeof value.allow_until !== 'string') delete value.allow_until;
        resolve(value);
      } catch {
        resolve({ worker, verdict: 'unknown', reason: 'CLI の応答が不正です' });
      }
    });
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      resolve({ worker, verdict: 'unknown', reason: 'CLI がタイムアウトしました' });
    }, 5000);
  });
}

export function createGuard({ env = process.env, home = homedir(), notify = (message) => console.warn(message), executable = join(home, '.local/bin/orca-agent-gate'), fs = { open }, configTimeoutMs = 5000 } = {}) {
  const pending = new Map();
  const last = new Map();
  const invalidWorkers = new Map();
  const shared = { waiters: null };
  let lastConfigError = null;
  return async (request) => {
    if (!env.ORCA_PANE_KEY || env.ORCA_GATE_HOOK === 'off') return;
    const provider = request?.model?.providerID;
    if (typeof provider !== 'string' || !namePattern.test(provider)) return;
    if (env.OPENCODE_QUOTA_WORKERS !== undefined) lastConfigError = null;
    const worker = await workerFor(provider, env, home, (error) => {
      if (error && error !== lastConfigError) notify('残量ガードの設定を読み込めないため、対応表なしで続行します。');
      lastConfigError = error;
    }, fs, configTimeoutMs, shared);
    if (worker === undefined) { invalidWorkers.delete(provider); return; }
    if (!validWorker(worker)) {
      const state = JSON.stringify(worker);
      if (!invalidWorkers.has(provider) || invalidWorkers.get(provider) !== state) {
        notify(`残量の判定が unknown（${provider}・worker の指定が不正です）のため注意して続行します。`);
      }
      invalidWorkers.set(provider, state);
      return;
    }
    invalidWorkers.delete(provider);
    let inflight = pending.get(worker);
    if (!inflight) {
      inflight = query(executable, worker);
      pending.set(worker, inflight);
      inflight.finally(() => { if (pending.get(worker) === inflight) pending.delete(worker); });
    }
    const result = await inflight;
    const state = `${result.verdict}:${result.allowed === true}`;
    const changed = last.get(worker) !== state;
    last.set(worker, state);
    const reason = result.reason || '理由なし';
    if (result.verdict === 'stop' && result.allowed !== true) {
      throw new Error(`残量の判定が stop（${worker}・${reason}）のため送りません。会話はそのまま。回復後に送り直すか orca-agent-gate allow ${worker}`);
    }
    if (changed && result.verdict === 'stop' && result.allowed === true) notify(`残量の判定は stop（${worker}・${reason}）ですが、一時許可により続行します。`);
    if (changed && (result.verdict === 'reserve' || result.verdict === 'unknown')) notify(`残量の判定が ${result.verdict}（${worker}・${reason}）のため注意して続行します。`);
  };
}

export default {
  id: 'orca-quota-guard',
  async setup(ctx) {
    if (!process.env.ORCA_PANE_KEY || process.env.ORCA_GATE_HOOK === 'off' || typeof ctx.session?.hook !== 'function') return;
    const registration = await ctx.session.hook('model.request', createGuard());
    return async () => { await registration?.dispose?.(); };
  },
};
