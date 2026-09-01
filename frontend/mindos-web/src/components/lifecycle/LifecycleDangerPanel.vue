<script setup lang="ts">
/** P15-04/05：删除影响预览、依赖决策与回收/永久清除的统一入口。 */
import { computed, ref } from 'vue'
import { api, type BlockingDependency, type DeletionImpact, type DependencyActionPayload, type LifecycleTargetType } from '@/services/api'

const props = defineProps<{
  targetType: LifecycleTargetType
  targetId: string
  targetTitle: string
  recycled: boolean
  compact?: boolean
}>()
const emit = defineEmits<{ (e: 'completed', action: 'recycle' | 'purge' | 'unrecycle'): void }>()

const impact = ref<DeletionImpact | null>(null)
const mode = ref<'recycle' | 'purge' | null>(null)
const loading = ref(false)
const executing = ref(false)
const error = ref('')
const choices = ref<Record<string, string>>({})
const replacementType = ref<Record<string, 'material' | 'knowledge'>>({})
const replacementId = ref<Record<string, string>>({})

function key(dep: BlockingDependency) { return `${dep.type}:${dep.id}` }
function label(action: string) {
  return ({ recycle: '移至回收站', archive: '归档纠错记录', detachSource: '移除待删除来源', replaceSource: '替换为其它来源', discard: '丢弃草稿' } as Record<string, string>)[action] ?? action
}
function dependencyTypeLabel(dep: BlockingDependency) {
  return ({ knowledge: '知识卡片', correction: '纠错记录', draft: '内容草稿', editDraft: '卡片修改草稿', pendingUpdate: '待发布版本' } as Record<string, string>)[dep.type] ?? '依赖项'
}

const unresolved = computed(() => (impact.value?.blockingDependencies ?? []).filter((dep) => {
  const action = choices.value[key(dep)]
  if (!action) return true
  return action === 'replaceSource' && !replacementId.value[key(dep)]?.trim()
}))

function defaultChoice(dep: BlockingDependency) {
  // 不自动代替用户做不可逆决定；仅为选择器提供可见的空状态。
  return choices.value[key(dep)] ?? ''
}

async function open(nextMode: 'recycle' | 'purge') {
  if (loading.value || executing.value) return
  const requestMode = nextMode
  loading.value = true; error.value = ''; mode.value = nextMode
  choices.value = {}; replacementType.value = {}; replacementId.value = {}
  try {
    impact.value = props.targetType === 'material'
      ? await api.getMaterialDeletionImpact(props.targetId)
      : await api.getKnowledgeDeletionImpact(props.targetId)
    if (mode.value !== requestMode) impact.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法获取删除影响预览'
    mode.value = null
  } finally { loading.value = false }
}

function buildActions(): DependencyActionPayload[] {
  return (impact.value?.blockingDependencies ?? []).map((dep) => {
    const depKey = key(dep)
    const action = choices.value[depKey]
    const payload: DependencyActionPayload = { type: dep.type, id: dep.id, action }
    if (action === 'replaceSource') payload.replacementSource = {
      sourceType: replacementType.value[depKey] ?? 'material', id: replacementId.value[depKey].trim(),
    }
    return payload
  })
}

async function execute() {
  if (!impact.value || unresolved.value.length || !mode.value || executing.value) return
  executing.value = true; error.value = ''
  const executingMode = mode.value
  try {
    const payload = { confirmToken: impact.value.confirmToken, dependencyActions: buildActions(), expectedRevision: impact.value.expectedRevision }
    if (executingMode === 'recycle') {
      props.targetType === 'material'
        ? await api.recycleMaterial(props.targetId, payload)
        : await api.recycleKnowledge(props.targetId, payload)
    } else {
      props.targetType === 'material'
        ? await api.purgeMaterial(props.targetId, payload)
        : await api.purgeKnowledge(props.targetId, payload)
    }
    const done = executingMode
    mode.value = null
    impact.value = null
    error.value = ''
    emit('completed', done)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作失败；请重新获取影响预览后重试'
  } finally { executing.value = false }
}

async function restore() {
  if (executing.value) return
  executing.value = true; error.value = ''
  try {
    props.targetType === 'material'
      ? await api.unrecycleMaterial(props.targetId)
      : await api.unrecycleKnowledge(props.targetId)
    emit('completed', 'unrecycle')
  } catch (e) { error.value = e instanceof Error ? e.message : '恢复失败' }
  finally { executing.value = false }
}

function close() {
  if (executing.value) return
  mode.value = null; impact.value = null; error.value = ''
}
</script>

<template>
  <section class="lifecycle-danger" :class="{ 'lifecycle-danger--compact': compact }" aria-label="删除与回收">
    <template v-if="!compact">
      <h2>删除与回收</h2>
      <p v-if="recycled">该对象已在回收站。恢复后会重新回到正常列表和检索范围。</p>
      <p v-else>删除前会先预览关联影响；永久清除不可恢复。</p>
    </template>
    <p v-if="error" class="lifecycle-danger__error">{{ error }}</p>
    <div class="lifecycle-danger__buttons">
      <button v-if="recycled" class="secondary-btn sm" type="button" :disabled="executing" @click="restore">{{ executing ? '处理中…' : '恢复' }}</button>
      <button v-else class="secondary-btn sm" type="button" :disabled="loading || executing" @click="open('recycle')">移至回收站</button>
      <button class="danger-btn secondary-btn sm" type="button" :disabled="loading || executing" @click="open('purge')">{{ compact ? '彻底删除' : '永久清除' }}</button>
    </div>

    <div v-if="mode" class="lifecycle-danger__preview" role="region" aria-live="polite">
      <p v-if="loading">正在计算删除影响…</p>
      <template v-else-if="impact">
        <h3>{{ mode === 'purge' ? '永久清除影响确认' : '移至回收站影响确认' }}</h3>
        <p>目标：{{ targetTitle }}。将清理 {{ impact.cleanupSummary.vectors }} 个向量、{{ impact.cleanupSummary.derivedRecords }} 条派生数据。</p>
        <p v-if="!impact.blockingDependencies.length">没有需要处理的活跃依赖。</p>
        <div v-for="dep in impact.blockingDependencies" :key="key(dep)" class="lifecycle-danger__dependency">
          <strong>{{ dep.title }}</strong><span>{{ dependencyTypeLabel(dep) }}</span>
          <p v-if="!dep.allowedActions.length" class="lifecycle-danger__hint">该操作正在执行，完成或失败前不能删除。</p>
          <select v-else v-model="choices[key(dep)]" :aria-label="`处理 ${dep.title}`">
            <option value="">请选择处理方式</option>
            <option v-for="action in dep.allowedActions" :key="action" :value="action">{{ label(action) }}</option>
          </select>
          <template v-if="defaultChoice(dep) === 'replaceSource'">
            <select v-model="replacementType[key(dep)]" aria-label="替换来源类型">
              <option value="material">原材料</option><option value="knowledge">知识卡片</option>
            </select>
            <input v-model="replacementId[key(dep)]" maxlength="128" placeholder="替换来源 ID" aria-label="替换来源 ID">
          </template>
        </div>
        <p v-if="unresolved.length" class="lifecycle-danger__hint">请为全部 {{ unresolved.length }} 项阻塞依赖选择处理方式；替换来源还需填写有效 ID。</p>
        <div class="lifecycle-danger__buttons">
          <button class="secondary-btn sm" type="button" :disabled="executing" @click="close">取消</button>
          <button class="danger-btn secondary-btn sm" type="button" :disabled="executing || unresolved.length > 0" @click="execute">{{ executing ? '执行中…' : mode === 'purge' ? '确认永久清除' : '确认移至回收站' }}</button>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.lifecycle-danger { margin-top: 16px; padding: 14px; border: 1px solid #f3c2c2; border-radius: 8px; background: #fff8f8; }
.lifecycle-danger--compact { margin: 0; padding: 0; border: 0; background: transparent; }
.lifecycle-danger--compact .lifecycle-danger__buttons { margin-top: 0; }
.lifecycle-danger--compact .lifecycle-danger__preview { width: min(560px, 100%); margin-top: 12px; padding: 12px; border: 1px solid #f3c2c2; border-radius: 6px; background: #fff8f8; }
.lifecycle-danger h2, .lifecycle-danger h3 { margin: 0 0 8px; font-size: 14px; color: #9b2525; }
.lifecycle-danger p { margin: 6px 0; font-size: 13px; color: #606266; }
.lifecycle-danger__buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.lifecycle-danger__preview { margin-top: 12px; padding-top: 12px; border-top: 1px solid #f3c2c2; }
.lifecycle-danger__dependency { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 10px; align-items: center; margin-top: 8px; padding: 8px; border: 1px solid #f1dddd; border-radius: 6px; background: #fff; font-size: 13px; }
.lifecycle-danger__dependency strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lifecycle-danger__dependency span { color: #909399; font-size: 12px; }
.lifecycle-danger__dependency select, .lifecycle-danger__dependency input { grid-column: 1 / -1; min-height: 30px; padding: 4px 7px; border: 1px solid #dcdfe6; border-radius: 5px; font: inherit; }
.lifecycle-danger__error { color: #c43d3d !important; }.lifecycle-danger__hint { color: #a66a1f !important; }
</style>
