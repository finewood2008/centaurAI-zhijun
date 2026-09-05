<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { getLearningState, startLearning, suggestLearning, proposeLearning, resolveLearning,
  type GrowthDecision, type LearningState, type LearningEpisode, type LearningExpectation, type LearningComparison } from '@/services/api'

const props = defineProps<{ conversationId: string; decision: GrowthDecision }>()
const state = ref<LearningState | null>(null)
const episode = computed(() => state.value?.episode)
const selected = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const open = ref(false)
const before = reactive<LearningExpectation>({ situation: '', expected: '', alternative: '' })
const after = reactive<LearningComparison>({ comparison: 'unclear', reflection: '', content: '', exceptions: '', framing: 'context_only' })
const done = computed(() => !!episode.value && ['applied', 'kept', 'deferred'].includes(episode.value.status))
const canStart = computed(() => selected.value && Object.values(before).every(v => v.trim().length >= 2))
const canCompare = computed(() => after.reflection.trim().length >= 2 && after.content.trim().length >= 2)
let generation = 0
onBeforeUnmount(() => { generation++ })

function accept(value: LearningEpisode) {
  if (state.value) state.value.episode = value
  Object.assign(before, value.expectation)
  if (value.proposal) Object.assign(after, value.proposal)
  else after.content = value.snapshot.content
  open.value = true
}
async function load() {
  const token = ++generation
  try {
    const data = await getLearningState(props.conversationId)
    if (token !== generation) return
    state.value = data
    selected.value = data.candidates[0]?.id || ''
    if (data.episode) accept(data.episode)
  } catch (e) { if (token === generation) error.value = e instanceof Error ? e.message : '观察暂时没有加载出来' }
}
watch(() => [props.conversationId, props.decision.updatedAt], load, { immediate: true })
async function run(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await fn() }
  catch (e) { error.value = e instanceof DOMException && e.name === 'TimeoutError' ? '等待超时，请刷新核对保存状态；也可以手动填写，不会切换到外部模型。' : e instanceof Error ? e.message : '操作未完成，请重试' }
  finally { busy.value = false }
}
async function suggest() {
  const token = generation
  await run(async () => {
    const response = await suggestLearning(props.conversationId, episode.value ? { expectedRevision: episode.value.revision } : { claimId: selected.value })
    if (token !== generation) return
    if (episode.value) Object.assign(after, response.candidate)
    else Object.assign(before, response.candidate)
    notice.value = `${response.external ? '在线' : '本地'} AI · ${response.model} 草拟，还没有保存。请改成你的意思后再确认。`
  })
}
async function start() {
  const c = state.value?.candidates.find(c => c.id === selected.value)
  if (!c) return
  await run(async () => accept(await startLearning(props.conversationId, { ...before, claimId: c.id, claimUpdatedAt: c.updatedAt })))
}
async function compare() {
  if (!episode.value) return
  const payload = { comparison: after.comparison, reflection: after.reflection, content: after.content,
    exceptions: after.exceptions, framing: after.framing, expectedRevision: episode.value.revision }
  await run(async () => accept(await proposeLearning(props.conversationId, payload)))
}
async function resolve(action: 'apply' | 'keep' | 'defer') {
  if (!episode.value) return
  const payload = { action, expectedRevision: episode.value.revision, content: after.content,
    framing: after.framing, exceptions: after.exceptions }
  await run(async () => accept(await resolveLearning(props.conversationId, payload)))
}
</script>

<template>
  <section class="learning" data-testid="learning-card" aria-label="用这次经历校准理解">
    <button class="learning__heading" :aria-expanded="open" @click="open = !open">
      <span>用这次经历校准理解</span><span aria-hidden="true">{{ open ? '−' : '＋' }}</span>
    </button>
    <p class="learning__hint">情境校准 · 资料保存在本机，按任务授权处理 · 可跳过</p>
    <div v-if="open" class="learning__body" :aria-busy="busy">
      <p v-if="error" role="alert">{{ error }} <button v-if="!state" @click="load">重新加载</button></p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <template v-if="state && !episode">
        <p v-if="decision.status !== 'open'">这次没有事前观察记录，不补写成预测。仍可在复盘里记下经验，下次再验证。</p>
        <template v-else-if="state.candidates.length">
          <p>先留下一个可核对的预期，等事情发生后再看。它与“我希望的结果”是两回事。</p>
          <label>这次想观察的理解<select v-model="selected" :disabled="busy"><option v-for="c in state.candidates" :key="c.id" :value="c.id">{{ c.content }}</option></select></label>
          <button :disabled="busy" @click="suggest">{{ busy ? '正在整理…' : '让 AI 草拟观察问题' }}</button>
          <label>这次的具体情境<textarea v-model="before.situation" maxlength="1000" rows="2" :disabled="busy" placeholder="例如：熟悉的主题，有准备时间，但要面对陌生听众" /></label>
          <label>预计会怎样（可观察）<textarea v-model="before.expected" maxlength="1000" rows="2" :disabled="busy" placeholder="例如：准备后愿意分享，现场提问仍可能紧张" /></label>
          <label>什么结果会让我们重新想一想<textarea v-model="before.alternative" maxlength="1000" rows="2" :disabled="busy" placeholder="例如：即使充分准备，仍完全不愿参与" /></label>
          <button class="learning__primary" :disabled="busy || !canStart" @click="start">确认预期，开始观察</button>
        </template>
        <p v-else>这次还没找到相关且已确认的理解。可以先正常聊、记录结果，不必为了观察新增标签。</p>
      </template>
      <template v-if="episode">
        <p><strong>当时的理解</strong><br>{{ episode.snapshot.content }}</p>
        <p><strong>情境</strong><br>{{ episode.expectation.situation }}</p>
        <p><strong>事前预期 · 已冻结</strong><br>{{ episode.expectation.expected }}</p>
        <p class="learning__hint">可能挑战它的结果：{{ episode.expectation.alternative }}</p>
        <p v-if="decision.outcome"><strong>实际结果 · 你的记录</strong><br>{{ decision.outcome.result }}</p>
        <template v-if="done">
          <p role="status">{{ episode.status === 'applied' ? '已按你的确认修订。旧理解保留历史；新理解尚未校准贴合度。' : episode.status === 'kept' ? '保留原理解。这次结果仍在记录里，不增加贴合度或预测准确分。' : '先不判断。已保存观察，不会反复追问。' }}</p>
          <p v-if="episode.status === 'applied'">修订措辞：{{ episode.resolution?.content }}</p>
        </template>
        <template v-else-if="decision.outcome">
          <button :disabled="busy" @click="suggest">{{ busy ? '正在整理…' : '让 AI 帮我对照' }}</button>
          <label>与预期相比<select v-model="after.comparison" :disabled="busy"><option value="unclear">还无法判断</option><option value="matched">大致吻合</option><option value="different">和预期不同</option><option value="mixed">有吻合，也有例外</option></select></label>
          <label>差异与原因<textarea v-model="after.reflection" rows="2" maxlength="1000" :disabled="busy" /></label>
          <label>准备怎样修订理解<textarea v-model="after.content" rows="3" maxlength="120" :disabled="busy" /></label>
          <label>这条理解适用于<select v-model="after.framing" :disabled="busy"><option value="context_only">只适用于这类具体情境</option><option value="current">当前阶段的状态</option><option value="aspirational">希望成为的样子</option><option value="long_term">我认同的长期倾向</option></select></label>
          <label>例外或还不确定的地方<textarea v-model="after.exceptions" rows="2" maxlength="500" :disabled="busy" /></label>
          <button :disabled="busy || !canCompare" @click="compare">保存对照，暂不改写本体</button>
          <template v-if="episode.status === 'proposed'">
            <p class="learning__hint">下面的确认只修订这一条理解，不代表已经验证了长期规律，也不会提高贴合度。</p>
            <button class="learning__primary" :disabled="busy || !canCompare || after.content.trim() === episode.snapshot.content.trim()" @click="resolve('apply')">确认并修订这条理解</button>
          </template>
          <div class="learning__actions"><button :disabled="busy" @click="resolve('keep')">保留原理解</button><button :disabled="busy" @click="resolve('defer')">先不判断</button></div>
        </template>
        <template v-else><p>等真实结果回来再比较。事前预期不会被模型重写。</p><button :disabled="busy" @click="resolve('defer')">暂停这次观察</button></template>
      </template>
    </div>
  </section>
</template>

<style scoped>
.learning { border: 1px solid var(--ws-border-color-2, #e2ded4); border-radius: 8px; background: var(--ws-body-bg, #fffcf6); padding: 12px; font-size: 13px; line-height: 1.65; overflow-wrap: anywhere; }
.learning__heading { display: flex; justify-content: space-between; width: 100%; font-size: 15px; font-weight: 600; border: 0 !important; padding: 0 !important; text-align: left; }
.learning__hint { color: var(--ws-text-secondary-color, #686b66); font-size: 12px; }
.learning__body { display: grid; gap: 10px; }
.learning p { margin: 4px 0; white-space: pre-wrap; }
.learning label { display: grid; gap: 5px; }
.learning select, .learning textarea { width: 100%; box-sizing: border-box; min-width: 0; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 6px; padding: 7px; font: inherit; color: inherit; background: var(--ws-card-bg, white); }
.learning button { font: inherit; color: inherit; cursor: pointer; background: transparent; border: 1px solid var(--ws-border-color, #d8d3c8); padding: 6px 10px; border-radius: 6px; }
.learning button:disabled { opacity: .5; cursor: default; }
.learning button:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
.learning .learning__primary { color: var(--ws-primary-color, #a6452e); border-color: currentColor; }
.learning__actions { display: flex; flex-wrap: wrap; gap: 8px; }
[role=alert] { color: #a6452e; }
</style>
