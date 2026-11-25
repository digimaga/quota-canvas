const deepseek = 'deepseek-v4.1-flash';
const glm = 'glm-5.3-flash';
const peakLabel = '（平日ピーク時のみ）';

export function isDeepSeekPeak(date) {
  const day = date.getUTCDay();
  const hour = date.getUTCHours();
  return day >= 1 && day <= 5 && ((hour >= 1 && hour < 4) || (hour >= 6 && hour < 10));
}

function selectable(profile, model) {
  return (profile === 'opencode-go' || profile === 'opendesign') &&
    model?.providerID === profile &&
    (model.id === deepseek || (profile === 'opencode-go' && model.id === glm));
}

export default {
  id: 't3-go-model-policy',
  async setup(ctx) {
    const profile = process.env.T3_GO_MODEL_POLICY;
    if (profile === undefined) return;

    await ctx.session.hook('model.request', ({ model }) => {
      if (!selectable(profile, model)) {
        throw new Error('このT3 Codeプロバイダーで許可されていないモデルです。');
      }
      if (model.id === glm && !isDeepSeekPeak(new Date())) {
        throw new Error('GLM-5.3 FlashはDeepSeekのピーク時間帯（平日10–13時・15–19時 JST）のみ利用できます。');
      }
    });

    await ctx.model.transform((editor) => {
      for (const model of editor.list()) {
        if (!selectable(profile, model)) {
          editor.update(model.providerID, model.id, (draft) => { draft.enabled = false; });
        } else if (model.id === glm && !model.name.endsWith(peakLabel)) {
          editor.update(model.providerID, model.id, (draft) => {
            draft.name += peakLabel;
          });
        }
      }
    });
  },
};
