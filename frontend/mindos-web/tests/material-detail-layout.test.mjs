import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript, compileTemplate, compileStyle } from '@vue/compiler-sfc'

const source = await readFile(new URL('../src/pages/MaterialDetailPage.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'detail-layout' })
const template = compileTemplate({ source: descriptor.template.content, filename: 'MaterialDetailPage.vue', id: 'detail-layout', compilerOptions: { bindingMetadata: script.bindings } })
assert.deepEqual(template.errors, [])
for (const style of descriptor.styles) {
  assert.deepEqual(compileStyle({ source: style.content, id: 'detail-layout', scoped: true }).errors, [])
}
for (const label of ['状态', '类型', '版本', '解析字符', '文件大小', '导入时间', '索引可用', '敏感识别']) {
  assert.ok(source.includes(`<span>${label}</span>`), `overview includes ${label}`)
}
const sections = ['aria-label="材料概览"', 'class="detail-panel preview-panel"', 'class="detail-panel parsed-content-panel"',
  'class="detail-panel image-panel"', 'class="detail-panel tag-panel"', 'class="detail-panel related-panel"',
  'aria-label="知识卡片管理"', 'class="detail-panel version-panel"', 'class="detail-panel lifecycle-panel"']
for (let index = 1; index < sections.length; index++) {
  assert.ok(source.indexOf(sections[index - 1]) < source.indexOf(sections[index]), `${sections[index]} follows ${sections[index - 1]}`)
}
assert.match(source, /<PdfPreview :src="mainPreviewUrl"/)
assert.match(source, /productPreview\(value\.previewUrl, previewController\.signal\)/)
assert.match(source, /saveProductResource\(value\.previewUrl, value\.fileName\)/)
assert.doesNotMatch(descriptor.template.content, /<iframe|:src="detail\.previewUrl"[^>]*<\/iframe/)
assert.doesNotMatch(source, /api\.(getWebRagConfig|uploadRagMaterialVersion|retryMaterialSensitiveScan)/)
assert.match(source, /api\.uploadMaterialVersion\(oldMaterialId/)
assert.match(source, /:disabled="draftDirty \|\| savingDraft \|\| confirmingDraft"/)
assert.match(source, /<div class="detail-actions draft-actions">/)
console.log('material detail layout: compiled, ordered, guarded and scoped')
