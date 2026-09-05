// 认识论标签 → 文字徽章回归：四种标记、引用 chip、无关文本不动、不放行原始 HTML。
// 运行：node --experimental-strip-types tests/labels.test.mjs
import assert from 'node:assert/strict'
import { decorateLabels, stripContextCitations, stripLabels, LAYER_MARKERS } from '../src/shared/labels.ts'

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

// 5) pN 是内部审计编号：支持多位、连续引用和异常编号，只清理编号占用的空格。
// MessageBubble 只对 Markdown 的普通 text token 调用它，因此代码 token 不受影响。
{
  assert.equal(stripContextCitations('产品方向 [p2][p10]。团队判断 [p1]'), '产品方向。团队判断')
  assert.equal(stripContextCitations('Use [p1] because it matters.'), 'Use because it matters.')
  assert.equal(stripContextCitations('[p0]异常编号也不展示'), '异常编号也不展示')
  assert.equal(stripContextCitations('普通 [x] 和材料 [m2] 保留'), '普通 [x] 和材料 [m2] 保留')
  assert.equal(stripContextCitations('普通文本  的空格，不应被改动。'), '普通文本  的空格，不应被改动。')
}

// 6) stripLabels 去掉用户可见摘要中的认识论标记与全部内部引用。
{
  assert.equal(stripLabels('【资料里看到的】你去年换过城市 [m2] [p12]。'), '你去年换过城市。')
}

console.log('labels: 6 tests OK')
