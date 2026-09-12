<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { api, type RedactionStatus } from '@/services/api'
import { shouldRefreshDetailAfterRedactionTransition } from '@/composables/redactionTransition'

const props = defineProps<{ materialId: string }>()
const emit = defineEmits<{ updated: [] }>()
const status = ref<RedactionStatus | null>(null)
const error = ref('')
const busy = ref(false)
const reviewLoading = ref(false)
const preview = ref<Awaited<ReturnType<typeof api.getRedactionReview>> | null>(null)
const reason = ref('')
const correctionSpans = ref<Array<{ start: number; end: number; type: string; required: boolean }>>([])
let epoch = 0
let timer: ReturnType<typeof setTimeout> | undefined
let polls = 0
let requestSerial = 0
let reviewSerial = 0
const labels: Record<string, string> = {
  pending: '等待隐私处理', processing: '正在处理，请稍候', pending_summary: '安全正文已就绪，准备生成摘要',
  ready: '安全正文与摘要已就绪', review_required: '需要人工复核', failed: '处理失败，可以重试',
  rejected: '本次结果已拒绝', suspended: '材料已暂停或回收', stale: '来源已变化，等待重新处理', off: '未开启隐私处理',
}
async function refresh(current = epoch) {
  const serial = ++requestSerial
  try {
    const next = await api.getRedactionStatus(props.materialId)
    if (current !== epoch || serial !== requestSerial) return
    const oldState = status.value?.state
    status.value = next
    error.value = ''
    if (shouldRefreshDetailAfterRedactionTransition(oldState, next.state)) emit('updated')
    if (['pending', 'processing', 'pending_summary', 'stale'].includes(next.state) && polls++ < 120) {
      timer = setTimeout(() => { void refresh(current) }, 3000)
    }
  } catch (e) {
    if (current === epoch && serial === requestSerial) error.value = e instanceof Error ? e.message : '隐私状态读取失败，请重试'
  }
}

function manualRefresh() {
  clearTimeout(timer)
  polls = 0
  void refresh()
}

async function act(action: () => Promise<unknown>) {
  if (busy.value) return
  const current = epoch
  busy.value = true
  clearTimeout(timer)
  try {
    await action()
    if (current !== epoch) return
    preview.value = null
    reason.value = ''
    polls = 0
    await refresh(current)
    if (current === epoch) emit('updated')
  } catch (e) {
    if (current === epoch) error.value = e instanceof Error ? e.message : '操作失败，未提交成功'
  } finally {
    if (current === epoch) busy.value = false
  }
}

async function openReview(kind: string) {
  if (reviewLoading.value || busy.value) return
  const current = epoch
  const serial = ++reviewSerial
  preview.value = null
  error.value = ''
  reviewLoading.value = true
  try {
    const result = await api.getRedactionReview(props.materialId, kind)
    if (current === epoch && serial === reviewSerial) {
      preview.value = result
      correctionSpans.value = result.replacements.map((item) => ({ ...item }))
    }
  } catch (e) {
    if (current === epoch) error.value = e instanceof Error ? e.message : '无法读取复核内容'
  } finally {
    if (current === epoch && serial === reviewSerial) reviewLoading.value = false
  }
}

function closeReview() {
  reviewSerial++
  preview.value = null
  correctionSpans.value = []
  reason.value = ''
  reviewLoading.value = false
}

function addCorrection() {
  correctionSpans.value.push({ start: 0, end: 1, type: 'person', required: false })
}

function removeCorrection(index: number) {
  if (!correctionSpans.value[index]?.required) correctionSpans.value.splice(index, 1)
}

function submitCorrection() {
  const value = preview.value
  if (!value || !reason.value.trim()) return
  const key = `redaction-correction-${value.attemptId}-${globalThis.crypto?.randomUUID?.() || Date.now()}`
  void act(() => api.correctRedaction(props.materialId, {
    versionId: value.versionId, attemptId: value.attemptId, kind: value.kind,
    expectedInputHash: value.inputHash,
    spans: correctionSpans.value.map(({ start, end, type }) => ({ start, end, type })),
    reason: reason.value.trim(),
  }, key))
}

function decide(decision: 'approve' | 'reject') {
  const value = preview.value
  if (!value || !reason.value.trim()) return
  void act(() => api.reviewRedaction(props.materialId, {
    versionId: value.versionId, attemptId: value.attemptId, decision, reason: reason.value.trim(),
  }))
}

watch(() => props.materialId, () => {
  epoch++; polls = 0; clearTimeout(timer)
  status.value = null; preview.value = null; correctionSpans.value = []; reason.value = ''; error.value = ''; busy.value = false; reviewLoading.value = false
  void refresh()
}, { immediate: true })
onBeforeUnmount(() => {
  epoch++
  reviewSerial++
  clearTimeout(timer)
  preview.value = null
  correctionSpans.value = []
  reason.value = ''
})
</script>

<template>
  <section class="detail-panel privacy-panel">
    <h3>隐私处理</h3>
    <p role="status">{{ status ? (labels[status.state] ?? '正在核对处理状态') : '正在读取状态…' }}</p>
    <p>只有通过检查的正文与摘要可用于知识卡片。原件仍保留，查看原件需要单独授权。</p>
    <p v-if="error" role="alert" class="privacy-error">{{ error }}</p>
    <button type="button" :disabled="busy" @click="manualRefresh">刷新状态</button>
    <template v-for="attempt in status?.attempts ?? []" :key="attempt.attempt_id">
      <button v-if="['failed', 'rejected'].includes(attempt.state) && status?.versionId" type="button" :disabled="busy"
        @click="act(() => api.retryRedaction(materialId, { versionId: status!.versionId!, kind: attempt.kind }))">
        重试{{ attempt.kind === 'body' ? '正文处理' : '摘要生成' }}
      </button>
      <button v-if="attempt.state === 'review_required' && status?.canReview" type="button" :disabled="busy || reviewLoading" @click="openReview(attempt.kind)">{{ reviewLoading ? '正在读取复核内容…' : '查看并复核' }}</button>
    </template>
    <p v-if="status?.state === 'review_required' && !status.canReview">请具有原件查看和复核权限的管理员处理。</p>
    <p v-if="status?.canReadOriginal" class="privacy-authorized">当前账号已获原件查看授权，可在上方“原始资料”区域预览。</p>
    <div v-if="preview">
      <button type="button" :disabled="busy" @click="closeReview">关闭复核/清除复核内容</button>
      <p>以下为待复核候选，可能仍包含敏感信息。无法确认已遮盖时请选择拒绝，不要批准。</p>
      <pre class="candidate">{{ preview.text }}</pre>
      <template v-if="preview.canCorrect">
        <details>
          <summary>按原文位置补充遮盖范围</summary>
          <p>位置采用原文字符序号（起始包含、结束不包含）。系统已识别的范围不能移除。</p>
          <pre class="candidate original">{{ preview.originalText }}</pre>
          <div v-for="(span, index) in correctionSpans" :key="`${index}-${span.start}-${span.end}`" class="correction-row">
            <label>开始 <input v-model.number="span.start" type="number" min="0" :max="preview.originalText.length" :disabled="busy || span.required" /></label>
            <label>结束 <input v-model.number="span.end" type="number" min="1" :max="preview.originalText.length" :disabled="busy || span.required" /></label>
            <label>类型 <select v-model="span.type" :disabled="busy || span.required">
              <option value="person">姓名</option><option value="address">地址</option><option value="organization">单位</option>
              <option value="case_identifier">案号</option><option value="credential">凭据</option><option value="identity_number">证件号</option>
              <option value="bank_card">银行卡</option><option value="passport">护照</option><option value="phone">电话</option><option value="email">邮箱</option>
            </select></label>
            <button v-if="!span.required" type="button" :disabled="busy" @click="removeCorrection(index)">移除</button>
            <span v-else>系统必选</span>
          </div>
          <button type="button" :disabled="busy" @click="addCorrection">新增范围</button>
          <button type="button" :disabled="busy || !reason.trim() || !correctionSpans.length" @click="submitCorrection">提交修正并继续处理</button>
        </details>
      </template>
      <label>复核说明 <textarea v-model="reason" maxlength="500" :disabled="busy" placeholder="说明判断依据，请勿填写敏感原值" /></label>
      <button type="button" :disabled="busy || !reason.trim()" @click="decide('approve')">确认通过</button>
      <button type="button" :disabled="busy || !reason.trim()" @click="decide('reject')">拒绝本次结果</button>
    </div>
  </section>
</template>

<style scoped>
.privacy-panel { margin: 16px 0; padding: 20px; }
.privacy-panel h3 { margin-top: 0; }
button { margin: 6px 8px 6px 0; }
.candidate { white-space: pre-wrap; max-height: 400px; overflow: auto; }
.candidate.original { border: 1px solid #f0b4ae; padding: 8px; }
.correction-row { display: flex; gap: 8px; align-items: end; flex-wrap: wrap; margin: 8px 0; }
.correction-row input { width: 110px; }
.privacy-error { color: #b42318; }
.privacy-authorized { color: #397552; }
textarea { display: block; width: 100%; min-height: 70px; }
</style>
