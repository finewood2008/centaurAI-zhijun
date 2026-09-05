<script setup lang="ts">
// 本体页的理解卡：默认只露「层标签 + 一句话 + 从哪来 + 两个动作」；「详情」展开后才有谓词、证据、置信、导出开关。
// working：对 / 不对 / ···（部分对 · 只适用于这件事 · 先别存）；confirmed：还是这样 / 不再这样了（需确认）/ ···（修正）。
import { ref, watch } from 'vue'
import { setClaimExport, type Claim, type ReviewAction } from '@/services/api'
import { useToast } from '@/composables/useToast'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import MoreMenu from '@/components/ui/MoreMenu.vue'
import AlignmentCard from '@/components/ontology/AlignmentCard.vue'
import { formatDay, layerMeta, predicateLabel, sectionLabel, sourceLine, trustMeta, REVIEW_MORE, REVIEW_PRIMARY } from '@/shared/ontology'

const props = defineProps<{
  claim: Claim
  busy?: boolean
  showSection?: boolean
}>()

const emit = defineEmits<{ (e: 'review', action: ReviewAction, editedContent?: string): void; (e: 'updated', claim: Claim): void }>()

const editing = ref(false)
const edited = ref('')
const retractOpen = ref(false)
const detailOpen = ref(false)

const CONFIRMED_MORE = [{ action: 'partial', label: '修正', hint: '改几个字，保留出处' }]

function act(action: ReviewAction) {
  if (props.busy) return
  if (action === 'partial') {
    editing.value = true
    edited.value = props.claim.content
    return
  }
  if (action === 'retract') {
    retractOpen.value = true
    return
  }
  emit('review', action)
}

function submitPartial() {
  const text = edited.value.trim()
  if (!text || text === props.claim.content) return
  emit('review', 'partial', text)
  editing.value = false
}

function confirmRetract() {
  retractOpen.value = false
  emit('review', 'retract')
}

// ---- 可带走（导出开关）：只对已确认理解；敏感 / 受限即使打开也不会出包，所以直接禁用。
const toast = useToast()
const exportOn = ref(Boolean(props.claim.exportAllowed))
const exportBusy = ref(false)
watch(() => props.claim.exportAllowed, (v) => { exportOn.value = Boolean(v) })
const exportLocked = () => props.claim.privacyLevel === 'sensitive' || props.claim.privacyLevel === 'restricted'

async function toggleExport() {
  if (exportBusy.value || exportLocked()) return
  const next = !exportOn.value
  exportOn.value = next
  exportBusy.value = true
  try {
    const updated = await setClaimExport(props.claim.id, next)
    exportOn.value = Boolean(updated.exportAllowed)
  } catch (err) {
    exportOn.value = !next
    toast({ type: 'error', message: err instanceof Error ? err.message : '导出开关保存失败' })
  } finally {
    exportBusy.value = false
  }
}

function evidenceLink(e: Claim['evidence'][number]): string | null {
  if (e.conversationId) return `/c/${encodeURIComponent(e.conversationId)}`
  if (e.materialId) return `/materials/${encodeURIComponent(e.materialId)}`
  return null
}

function evidenceKindLabel(kind: string): string {
  switch (kind) {
    case 'conversation_turn':
      return '对话'
    case 'material_span':
      return '资料'
    case 'user_edit':
      return '你的修改'
    case 'decision':
      return '判断'
    case 'review':
      return '复盘'
    default:
      return kind
  }
}
</script>

<template>
  <article class="zj-claim" :class="[`is-${claim.trustState}`, { 'is-open': detailOpen }]" :aria-busy="busy || undefined">
    <header class="zj-claim__head">
      <StatusBadge :meta="layerMeta(claim.layer)" />
      <span v-if="claim.scope === 'context_only'" class="zj-seal zj-seal--muted">只适用于当时那件事</span>
      <span v-if="claim.challenged && claim.trustState === 'working'" class="zj-seal zj-seal--warning" :title="claim.challengeNote || '与另一条理解矛盾'">有矛盾</span>
      <span v-if="showSection" class="zj-claim__section">{{ sectionLabel(claim.section) }}</span>
      <button type="button" class="zj-claim__detail-btn" :aria-expanded="detailOpen" @click="detailOpen = !detailOpen">{{ detailOpen ? '收起' : '详情' }}</button>
    </header>

    <p v-if="!editing" class="zj-claim__content">{{ claim.content }}</p>
    <div v-else class="zj-claim__edit">
      <label :for="`claim-edit-${claim.id}`" class="zj-claim__edit-label">改成更准确的说法</label>
      <textarea :id="`claim-edit-${claim.id}`" v-model="edited" class="zj-claim__textarea" rows="2" maxlength="120" />
      <div class="zj-claim__edit-actions">
        <BaseButton size="sm" variant="primary" :disabled="busy || !edited.trim()" @click="submitPartial">保存修正</BaseButton>
        <BaseButton size="sm" variant="text" @click="editing = false">取消</BaseButton>
      </div>
    </div>

    <p class="zj-claim__source">{{ sourceLine(claim) }}</p>
    <div v-if="claim.contextual" class="zj-claim__details">
      <strong>来自真实经历的修订 · 仅本机</strong>
      <p>适用情境：{{ claim.contextual.situation }}</p>
      <p v-if="claim.contextual.exceptions">例外与未知：{{ claim.contextual.exceptions }}</p>
      <p>这不是潜意识真实性评分；一次经历不证明长期规律。</p>
    </div>
    <AlignmentCard v-if="claim.trustState === 'confirmed'" :claim="claim" @updated="emit('updated', $event)" @refreshed="emit('updated', $event)" />

    <div v-if="detailOpen" class="zj-claim__details">
      <p class="zj-claim__meta">
        <StatusBadge :meta="trustMeta(claim.trustState)" />
        <span v-if="claim.promotionReady" class="zj-seal zj-seal--green" title="至少两个独立来源提到过">多处提到</span>
        <span class="zj-claim__conf" title="系统对抽取这条记录的把握，不代表内心真实性">记录置信 {{ Math.round(claim.confidence * 100) }}%</span>
      </p>
      <p class="zj-claim__names">
        <span>{{ claim.subjectName }}</span>
        <span v-if="predicateLabel(claim.predicate)" class="zj-claim__pred">{{ predicateLabel(claim.predicate) }}</span>
        <span v-if="claim.objectName">{{ claim.objectName }}</span>
      </p>
      <ul v-if="claim.evidence.length" class="zj-claim__evidence">
        <li v-for="e in claim.evidence" :key="e.id">
          <span class="zj-claim__ev-kind">{{ evidenceKindLabel(e.kind) }}</span>
          <span v-if="e.quote" class="zj-claim__quote">「{{ e.quote }}」</span>
          <RouterLink v-if="evidenceLink(e)" :to="evidenceLink(e)!" class="zj-claim__ev-link">查看来源</RouterLink>
        </li>
      </ul>
      <div v-if="claim.trustState === 'confirmed'" class="zj-claim__export">
        <button
          type="button"
          role="switch"
          class="zj-claim__switch"
          :class="{ 'is-on': exportOn && !exportLocked() }"
          :aria-checked="exportOn && !exportLocked()"
          :disabled="busy || exportBusy || exportLocked()"
          :title="exportLocked() ? '敏感或受限的理解永远不会出设备' : '打开后，其他 Agent 取上下文包时可以拿到这一条'"
          @click="toggleExport"
        >
          <span class="zj-claim__switch-knob" aria-hidden="true" />
          <span class="zj-claim__switch-label">{{ exportLocked() ? '敏感内容不会带走' : exportOn ? '可带走' : '不带走' }}</span>
        </button>
      </div>
      <p class="zj-claim__time">第一次记下 {{ formatDay(claim.firstSeen) }} · 最近一次确认 {{ formatDay(claim.lastReaffirmed) }}</p>
    </div>

    <footer v-if="!editing" class="zj-claim__foot">
      <div class="zj-claim__actions">
        <template v-if="claim.trustState === 'working'">
          <button
            v-for="item in REVIEW_PRIMARY"
            :key="item.action"
            type="button"
            class="zj-claim__btn"
            :class="{ 'is-primary': item.action === 'confirm', 'is-danger': item.action === 'reject' }"
            :disabled="busy"
            @click="act(item.action)"
          >{{ item.label }}</button>
          <MoreMenu :items="REVIEW_MORE" :disabled="busy" label="其他处理" @select="(a) => act(a as ReviewAction)" />
        </template>
        <template v-else-if="claim.trustState === 'confirmed'">
          <button type="button" class="zj-claim__btn" :disabled="busy" title="现在还是这样，重新确认一次" @click="act('reaffirm')">还是这样</button>
          <button type="button" class="zj-claim__btn is-danger" :disabled="busy" title="不再把它当作对你的认识" @click="act('retract')">不再这样了</button>
          <MoreMenu :items="CONFIRMED_MORE" :disabled="busy" label="其他处理" @select="(a) => act(a as ReviewAction)" />
        </template>
      </div>
    </footer>

    <ConfirmDialog
      :open="retractOpen"
      title="不再这样了？"
      :message="`知君不会再把「${claim.content}」当作对你的认识，也不会再在回答里提起它。`"
      confirm-text="确定"
      danger
      @confirm="confirmRetract"
      @cancel="retractOpen = false"
    />
  </article>
</template>

<style scoped>
.zj-claim {
  padding: 14px 16px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-claim.is-working {
  border-style: dashed;
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-claim.is-retracted,
.zj-claim.is-superseded {
  opacity: 0.7;
}
.zj-claim__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__section {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-claim__detail-btn {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-claim__detail-btn:hover {
  color: var(--ws-primary-color, #a6452e);
}
.zj-claim__content {
  margin: 0 0 4px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--ws-text-primary-color, #1d211f);
}
.is-retracted .zj-claim__content {
  text-decoration: line-through;
}
.zj-claim__source {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__details {
  display: grid;
  gap: 6px;
  margin: 0 0 10px;
  padding: 10px 12px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
}
.zj-claim__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__conf {
  margin-left: auto;
}
.zj-claim__names {
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.zj-claim__pred {
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
.zj-claim__evidence {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: var(--ws-text-color, #3c403d);
}
.zj-claim__evidence li {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  line-height: 1.7;
}
.zj-claim__ev-kind {
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
.zj-claim__quote {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__export {
  margin: 2px 0 0;
}
.zj-claim__switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  border: none;
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-claim__switch-knob {
  position: relative;
  width: 30px;
  height: 16px;
  border-radius: 999px;
  background: var(--ws-switch-bg, #d8d3c8);
  transition: background 0.15s;
}
.zj-claim__switch-knob::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  transition: transform 0.15s;
}
.zj-claim__switch.is-on .zj-claim__switch-knob {
  background: var(--ws-success-color, #4a7c59);
}
.zj-claim__switch.is-on .zj-claim__switch-knob::after {
  transform: translateX(14px);
}
.zj-claim__switch.is-on .zj-claim__switch-label {
  color: var(--ws-success-color, #4a7c59);
  font-weight: 600;
}
.zj-claim__switch:disabled {
  cursor: not-allowed;
  opacity: 0.7;
}
.zj-claim__time {
  margin: 0;
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-claim__foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.zj-claim__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.zj-claim__btn {
  padding: 4px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
.zj-claim__btn:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-claim__btn.is-primary {
  background: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-white, #fff);
}
.zj-claim__btn.is-danger {
  color: var(--ws-danger-color, #a6452e);
}
.zj-claim__btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.zj-claim__edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}
.zj-claim__edit-label {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-body-bg, #fffcf6);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  resize: vertical;
}
.zj-claim__textarea:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-claim__edit-actions {
  display: flex;
  gap: 8px;
}
</style>
