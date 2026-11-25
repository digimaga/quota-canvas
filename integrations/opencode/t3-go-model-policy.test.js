import assert from 'node:assert/strict';
import { test } from 'node:test';
import plugin, { isDeepSeekPeak } from './t3-go-model-policy.js';

test('DeepSeek のピークは平日の UTC 01–04 時と 06–10 時', () => {
  for (const [time, expected] of [
    ['2026-11-23T00:59:59Z', false],
    ['2026-11-23T01:00:00Z', true],
    ['2026-11-23T04:00:00Z', false],
    ['2026-11-23T06:00:00Z', true],
    ['2026-11-27T09:59:59Z', true],
    ['2026-11-27T10:00:00Z', false],
    ['2026-11-28T07:00:00Z', false],
  ]) {
    assert.equal(isDeepSeekPeak(new Date(time)), expected, time);
  }
});

test('OpenDesign Go の要求ではモデル ID と提供元の両方を確認する', async (t) => {
  const previous = process.env.T3_GO_MODEL_POLICY;
  t.after(() => {
    if (previous === undefined) delete process.env.T3_GO_MODEL_POLICY;
    else process.env.T3_GO_MODEL_POLICY = previous;
  });
  process.env.T3_GO_MODEL_POLICY = 'opendesign';
  let check;
  await plugin.setup({
    session: { hook: (_name, handler) => { check = handler; } },
    model: { transform() {} },
  });
  assert.doesNotThrow(() => check({ model: { providerID: 'opendesign', id: 'deepseek-v4.1-flash' } }));
  assert.throws(() => check({ model: { providerID: 'opencode-go', id: 'deepseek-v4.1-flash' } }));
  assert.throws(() => check({ model: { providerID: 'opendesign', id: 'glm-5.3-flash' } }));
});
