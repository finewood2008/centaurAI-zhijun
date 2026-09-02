// 认识论标签 → 文字徽章回归：四种标记、引用 chip、无关文本不动、不放行原始 HTML。
// 运行：node --experimental-strip-types tests/labels.test.mjs
import assert from 'node:assert/strict'
import { decorateLabels, stripLabels, LAYER_MARKERS } from '../src/shared/labels.ts'

// 1) 四种标记都被替换成带文字的 span（不能只靠颜色）
{
  const input = `<p>${LAYER_MARKERS.map((m) => m.marker).join(' ')}</p>`
  const out = decorateLabels(input)
  for (const { kind, text } of LAYER_MARKERS) {
    assert.match(out, new RegExp(`<span class="layer-badge layer-badge--${kind}">${text}</span>`))
  }
  assert.doesNotMatch(out, /【/)
}

// 2) 引用 chip：[m1]…[m9] 变成 a.cite-chip；[m10] 与普通中括号不动
{
  const out = decorateLabels('<p>见 [m1] 与 [m9]，但 [m10] 和 [x] 不变</p>')
  assert.match(out, /<a class="cite-chip" data-cite="1" href="#cite-1">m1<\/a>/)
  assert.match(out, /<a class="cite-chip" data-cite="9" href="#cite-9">m9<\/a>/)
  assert.match(out, /\[m10\]/)
  assert.match(out, /\[x\]/)
}

// 3) 无关文本保持原样
{
  const plain = '<p>今天先把这件事记下来。</p>'
  assert.equal(decorateLabels(plain), plain)
}

// 4) 输入里已转义的 HTML 不会被还原成标签（markdown-it html:false 之后的安全性不被破坏）
{
  const out = decorateLabels('&lt;script&gt;alert(1)&lt;/script&gt; 【知君的看法】不急')
  assert.doesNotMatch(out, /<script/)
  assert.match(out, /layer-badge--view/)
}

// 5) stripLabels 去掉标记与引用，用于纯文本摘要
{
  assert.equal(stripLabels('【资料里看到的】你去年换过城市 [m2]'), '你去年换过城市 ')
}

console.log('labels: 5 tests OK')
