import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const componentUrl = new URL('../src/components/conversation/RagSensitiveDialog.vue', import.meta.url)

async function loadSetup(initialProps) {
  const source = await readFile(componentUrl, 'utf8')
  const { descriptor } = parse(source, { filename: 'RagSensitiveDialog.vue' })
  const compiled = compileScript(descriptor, {
    id: 'rag-sensitive-dialog-test',
    genDefaultAs: '__default__',
  })
  let script = ts.transpileModule(compiled.content, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText

  script = script
    .replace(
      /import \{ defineComponent as _defineComponent \} from ['"]vue['"];?/,
      'const { defineComponent: _defineComponent } = __vue;',
    )
    .replace(
      /import \{ computed, ref, watch \} from ['"]vue['"];?/,
      'const { computed, ref, watch } = __vue;',
    )
    .replace(/import BaseButton from ['"]\.\.\/ui\/BaseButton\.vue['"];?/, 'const BaseButton = {};')
    .replace(/export \{ __default__ as default \};?/, '')

  script += '\nreturn __default__;'

  const component = new Function('__vue', script)(Vue)
  const props = Vue.reactive(initialProps)
  const events = []
  const exposed = component.setup(props, {
    expose() {},
    emit(name, ...args) {
      events.push([name, ...args])
    },
  })
  return { source, props, events, exposed }
}

test('confirmation only keeps the V2 redacted hit fields and guards original access', async () => {
  const { props, events, exposed } = await loadSetup({
    status: 'sensitive_confirmation_required',
    hits: [
      {
        category: 'mobile_phone',
        redactedPreview: '联系电话：138****0000',
        title: '客户登记表',
        location: { page: 2, paragraph: 3, secret: 'must-not-render' },
        text: '13812340000',
        materialId: 'material-secret',
      },
      {
        category: 'mobile_phone',
        redactedPreview: '联系电话：138****0000',
        title: '客户登记表',
        location: { page: 2, paragraph: 3 },
      },
    ],
    detectionNotice: undefined,
    canReadOriginal: false,
    busy: false,
  })

  assert.deepEqual(exposed.safeHits.value, [
    {
      category: 'mobile_phone',
      redactedPreview: '联系电话：138****0000',
      title: '客户登记表',
      location: { page: 2, paragraph: 3 },
    },
  ])
  assert.equal(exposed.categoryLabel('mobile_phone'), '手机号码')
  assert.equal(exposed.categoryLabel('unknown'), '敏感信息')
  assert.equal(exposed.locationLabel(exposed.safeHits.value[0].location), '第 2 页 · 第 3 段')

  exposed.chooseOriginal()
  assert.deepEqual(events, [])
  props.canReadOriginal = true
  exposed.chooseOriginal()
  exposed.chooseMasked()
  assert.deepEqual(events, [['original'], ['masked']])

  props.busy = true
  exposed.chooseOriginal()
  exposed.chooseMasked()
  exposed.cancel()
  assert.deepEqual(events, [['original'], ['masked']])
})

test('unavailable state supports passed-only, retry and an explicit two-step risk release', async () => {
  const { props, events, exposed } = await loadSetup({
    status: 'sensitive_check_unavailable',
    hits: [],
    detectionNotice: {
      message: '检测服务暂不可用',
      retrievedCount: 4,
      checkedCount: 2,
      withheldCount: 2,
      retryable: true,
      riskEligibleCount: 2,
      riskMessage: '放行可能泄露敏感信息',
    },
    canReadOriginal: false,
    passedCount: 2,
    riskAvailable: true,
    busy: false,
  })

  assert.equal(exposed.hasPassedHits.value, true)
  assert.equal(exposed.canRiskRelease.value, true)
  exposed.continueWithPassed()
  exposed.retryDetection()
  assert.deepEqual(events, [['continue-passed'], ['retry']])

  exposed.releaseRisk()
  assert.deepEqual(events, [['continue-passed'], ['retry']])
  exposed.requestRiskRelease()
  assert.equal(exposed.riskExpanded.value, true)
  assert.equal(exposed.riskAcknowledged.value, false)
  exposed.releaseRisk()
  assert.deepEqual(events, [['continue-passed'], ['retry']])

  exposed.riskAcknowledged.value = true
  exposed.releaseRisk()
  assert.deepEqual(events, [['continue-passed'], ['retry'], ['risk-release']])

  props.detectionNotice = { ...props.detectionNotice, riskEligibleCount: 3 }
  await Vue.nextTick()
  assert.equal(exposed.riskExpanded.value, false)
  assert.equal(exposed.riskAcknowledged.value, false)

  exposed.cancel()
  assert.deepEqual(events.at(-1), ['cancel'])
})

test('risk release follows server eligibility and busy blocks all actions', async () => {
  const { props, events, exposed } = await loadSetup({
    status: 'sensitive_check_unavailable',
    hits: [],
    detectionNotice: {
      checkedCount: 0,
      retryable: false,
      riskEligibleCount: 0,
    },
    canReadOriginal: false,
    passedCount: 0,
    riskAvailable: false,
    busy: false,
  })

  assert.equal(exposed.hasPassedHits.value, false)
  assert.equal(exposed.canRetry.value, false)
  assert.equal(exposed.canRiskRelease.value, false)
  exposed.continueWithPassed()
  exposed.retryDetection()
  exposed.requestRiskRelease()
  exposed.riskAcknowledged.value = true
  exposed.releaseRisk()
  assert.deepEqual(events, [])

  props.detectionNotice = {
    checkedCount: 1,
    retryable: true,
    riskEligibleCount: 1,
  }
  props.passedCount = 1
  props.riskAvailable = true
  props.busy = true
  await Vue.nextTick()
  exposed.continueWithPassed()
  exposed.retryDetection()
  exposed.requestRiskRelease()
  exposed.cancel()
  assert.deepEqual(events, [])
})

test('template does not render or persist original content and exposes the complete action contract', async () => {
  const source = await readFile(componentUrl, 'utf8')

  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|document\.cookie/)
  assert.doesNotMatch(source, /v-html/)
  assert.doesNotMatch(source, /\{\{\s*detectionNotice\?\.riskToken/)
  assert.doesNotMatch(source, /\{\{\s*[^}]*confirmToken/)
  assert.match(source, /redactedPreview/)
  assert.match(source, /role="alertdialog"/)
  assert.match(source, /type="checkbox"/)

  for (const eventName of [
    'masked',
    'original',
    'continue-passed',
    'retry',
    'risk-release',
    'cancel',
  ]) {
    assert.match(source, new RegExp(`['"]${eventName}['"]`))
  }

  assert.match(source, /riskExpanded\.value && riskAcknowledged\.value/)
})
