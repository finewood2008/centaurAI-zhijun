// 助手正文的认识论标签 → 文字徽章（纯字符串处理，可在 node 下测试）。
//
// 输入是 markdown-it（html:false）渲染后的 HTML：模型输出里的 `<` 已被转义，
// 因此这里只会注入我们自己的 span / a，不会把用户或模型的原始 HTML 放行。
// 四种标记与 docs/development/zhijun-api-contract.md §1 一致。
export const LAYER_MARKERS: ReadonlyArray<{ marker: string; kind: 'told' | 'material' | 'guess' | 'view'; text: string }> = [
  { marker: '【你告诉我的】', kind: 'told', text: '你告诉我的' },
  { marker: '【资料里看到的】', kind: 'material', text: '资料里看到的' },
  { marker: '【我推测的】', kind: 'guess', text: '我推测的' },
  { marker: '【知君的看法】', kind: 'view', text: '知君的看法' },
]

const CITE_RE = /\[m([1-9])\]/g

export function decorateLabels(html: string): string {
  let out = html
  for (const { marker, kind, text } of LAYER_MARKERS) {
    out = out.split(marker).join(`<span class="layer-badge layer-badge--${kind}">${text}</span>`)
  }
  out = out.replace(CITE_RE, (_m, n: string) => `<a class="cite-chip" data-cite="${n}" href="#cite-${n}">m${n}</a>`)
  return out
}

/** 去掉正文里的标记，用于会话列表/摘要等纯文本场景。 */
export function stripLabels(text: string): string {
  let out = text
  for (const { marker } of LAYER_MARKERS) out = out.split(marker).join('')
  return out.replace(CITE_RE, '')
}
