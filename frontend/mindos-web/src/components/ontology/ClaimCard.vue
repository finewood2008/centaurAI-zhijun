<script setup lang="ts">
// 本体页的理解卡：层标签 + 信任状态 + 内容 + 来源深链 + 动作。
// working：对 / 部分对 / 只适用于这件事 / 不对 / 先别存；confirmed：重申 / 撤回（撤回需确认弹窗）。
import { ref } from 'vue'
import type { Claim, ReviewAction } from '@/services/api'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { formatDate } from '@/shared/format'
import { layerMeta, sectionLabel, trustMeta, REVIEW_ACTIONS_WORKING } from '@/shared/ontology'

const props = defineProps<{
  claim: Claim
  busy?: boolean
  showSection?: boolean
}>()

const emit = defineEmits<{ (e: 'review', action: ReviewAction, editedContent?: string): void }>()

const editing = ref(false)
const edited = ref('')
const retractOpen = ref(false)

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
  <article class="zj-claim" :class="`is-${claim.trustState}`" :aria-busy="busy || undefined">
    <header class="zj-claim__head">
      <StatusBadge :meta="layerMeta(claim.layer)" />
      <StatusBadge :meta="trustMeta(claim.trustState)" />
      <span v-if="claim.scope === 'context_only'" class="zj-claim__scope">只适用于当时那件事</span>
      <span v-if="claim.promotionReady" class="zj-claim__flag zj-claim__flag--ready" title="至少两个独立来源提到过">多处提到</span>
      <span v-if="claim.challenged && claim.trustState === 'working'" class="zj-claim__flag zj-claim__flag--challenged" :title="claim.challengeNote || '与另一条理解矛盾'">有矛盾</span>
      <span v-if="showSection" class="zj-claim__section">{{ sectionLabel(claim.section) }}</span>
      <span class="zj-claim__conf" :title="`置信度 ${Math.round(claim.confidence * 100)}%`">置信 {{ Math.round(claim.confidence * 100) }}%</span>
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

    <p class="zj-claim__names">
      <span>{{ claim.subjectName }}</span>
      <span class="zj-claim__pred">{{ claim.predicate }}</span>
      <span v-if="claim.objectName">{{ claim.objectName }}</span>
    </p>

    <ul v-if="claim.evidence.length" class="zj-claim__evidence">
      <li v-for="e in claim.evidence" :key="e.id">
        <span class="zj-claim__ev-kind">{{ evidenceKindLabel(e.kind) }}</span>
        <span v-if="e.quote" class="zj-claim__quote">「{{ e.quote }}」</span>
        <RouterLink v-if="evidenceLink(e)" :to="evidenceLink(e)!" class="zj-claim__ev-link">查看来源</RouterLink>
      </li>
    </ul>

    <footer class="zj-claim__foot">
      <span class="zj-claim__time">首次 {{ formatDate(claim.firstSeen) }} · 最近重申 {{ formatDate(claim.lastReaffirmed) }}</span>
      <div v-if="!editing" class="zj-claim__actions">
        <template v-if="claim.trustState === 'working'">
          <button
            v-for="item in REVIEW_ACTIONS_WORKING"
            :key="item.action"
            type="button"
            class="zj-claim__btn"
            :class="{ 'is-primary': item.action === 'confirm', 'is-danger': item.action === 'reject' }"
            :disabled="busy"
            @click="act(item.action)"
          >{{ item.label }}</button>
        </template>
        <template v-else-if="claim.trustState === 'confirmed'">
          <button type="button" class="zj-claim__btn" :disabled="busy" @click="act('reaffirm')">重申</button>
          <button type="button" class="zj-claim__btn" :disabled="busy" @click="act('partial')">修正</button>
          <button type="button" class="zj-claim__btn is-danger" :disabled="busy" @click="act('retract')">撤回</button>
        </template>
      </div>
    </footer>

    <ConfirmDialog
      :open="retractOpen"
      title="撤回这条理解？"
      :message="`撤回后，知君不会再把「${claim.content}」当作对你的认识，也不会再在回答里复述它。`"
      confirm-text="撤回"
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
  background: var(--ws-body-bg, #fffcf6);
}
.zj-claim.is-working {
  border-style: dashed;
  border-color: var(--ws-warning-color, #b8862b);
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
.zj-claim__scope,
.zj-claim__section {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ws-card-bg, #f3efe6);
}
.zj-claim__flag {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.zj-claim__flag--ready {
  background: rgba(74, 124, 89, 0.12);
  color: var(--ws-success-color, #4a7c59);
}
.zj-claim__flag--challenged {
  background: rgba(184, 134, 43, 0.14);
  color: var(--ws-warning-color, #b8862b);
}
.zj-claim__conf {
  margin-left: auto;
}
.zj-claim__content {
  margin: 0 0 6px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--ws-text-primary-color, #1d211f);
}
.is-retracted .zj-claim__content {
  text-decoration: line-through;
}
.zj-claim__names {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.zj-claim__pred {
  padding: 0 6px;
  border-radius: 4px;
  background: var(--ws-card-bg, #f3efe6);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.zj-claim__evidence {
  margin: 0 0 8px;
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
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
.zj-claim__quote {
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-claim__foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}
.zj-claim__time {
  font-size: 11px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-claim__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-left: auto;
}
.zj-claim__btn {
  padding: 4px 12px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-color, #3c403d);
  font-size: 12px;
  font-weight: 600;
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
