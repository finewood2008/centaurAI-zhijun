import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { parse } from '@vue/compiler-sfc'

const source = await readFile(new URL('../src/components/conversation/RoutingPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const css = descriptor.styles.map(style => style.content).join('\n')
const rule = selector => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`))
  assert.ok(match, `missing ${selector}`)
  return match[1]
}

test('pending task wraps by available drawer width and keeps action labels on one line', () => {
  assert.match(rule('.routing-task'), /flex-wrap:\s*wrap/)
  assert.match(rule('.routing-task__content'), /flex:\s*1 1 16rem/)
  assert.match(rule('.routing-task__content'), /min-width:\s*0/)
  assert.match(rule('.routing-task__content'), /overflow-wrap:\s*anywhere/)
  const action = rule('.routing-settings .routing-task__action')
  assert.match(action, /flex:\s*0 0 auto/)
  assert.match(action, /white-space:\s*nowrap/)
  assert.match(action, /min-height:\s*40px/)
  assert.match(rule('.routing-task__title'), /flex-wrap:\s*wrap/)
})

test('layout keeps the original disabled guard and explicit retry action', () => {
  assert.match(descriptor.template.content, /type="button" class="routing-task__action" :disabled="busy \|\| disabled"/)
  assert.match(descriptor.template.content, /@click="pending\(task, !!task.previewExpired \|\| !!task.failedCount\)"/)
  for (const label of ['重新整理', '重新准备待办', '核对并继续']) {
    assert.ok(descriptor.template.content.includes(label))
  }
})
