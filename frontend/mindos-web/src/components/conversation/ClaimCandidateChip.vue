<script setup lang="ts">
// 对话内的候选理解 chip：一句候选 + 「对 / 不对」两个主动作；其余三个收进「···」并各带一句解释。
// 只有明确点击才改变本体状态；chip 本身只是展示。
import { ref } from 'vue'
import type { Claim, ReviewAction } from '@/services/api'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import MoreMenu from '@/components/ui/MoreMenu.vue'
import { layerMeta, sectionLabel, REVIEW_MORE, REVIEW_PRIMARY } from '@/shared/ontology'

const props = defineProps<{
  claim: Claim
  busy?: boolean
  dismissible?: boolean
}>()

const emit = defineEmits<{ (e: 'review', action: ReviewAction, editedContent?: string): void; (e: 'dismiss'): void }>()

const editing = ref(false)
const edited = ref('')

function onAction(action: ReviewAction) {
  if (props.busy) return
  if (action === 'partial') {
    editing.value = true
    edited.value = props.claim.content
    return
  }
  emit('review', action)
}

function submitPartial() {
  if (props.busy) return
  const text = edited.value.trim()
  if (!text || text === props.claim.content) return
  emit('review', 'partial', text)
  // The parent removes the card after a successful save. Keep edits on failure.
}
</script>

<template>
  <div class="zj-chip" :aria-busy="busy || undefined">
    <div class="zj-chip__head">
      <span class="zj-chip__lead">知君想记住</span>
      <StatusBadge :meta="layerMeta(claim.layer)" />
      <span class="zj-chip__section">{{ sectionLabel(claim.section) }}</span>
    </div>
    <p class="zj-chip__content">{{ claim.content }}</p>
    <p v-if="claim.evidence?.[0]?.quote" class="zj-chip__quote">你的原话：「{{ claim.evidence[0].quote }}」</p>
    <div v-if="!editing" class="zj-chip__actions">
      <button
        v-for="item in REVIEW_PRIMARY"
        :key="item.action"
        type="button"
        class="zj-chip__btn"
        :class="{ 'is-primary': item.action === 'confirm', 'is-danger': item.action === 'reject' }"
        :disabled="busy"
        @click="onAction(item.action)"
      >
        {{ item.label }}
      </button>
      <MoreMenu :items="REVIEW_MORE" :disabled="busy" label="其他处理" @select="(a) => onAction(a as ReviewAction)" />
      <button v-if="dismissible" type="button" class="zj-chip__btn" :disabled="busy" title="不保留这条候选，也不把这句话判为错误；原对话仍在" @click="emit('dismiss')">不用记住</button>
    </div>
    <div v-else class="zj-chip__edit">
      <label class="zj-chip__edit-label" :for="`edit-${claim.id}`">改成更准确的说法</label>
      <textarea :id="`edit-${claim.id}`" v-model="edited" :disabled="busy" class="zj-chip__textarea" rows="2" maxlength="120" />
      <div class="zj-chip__edit-actions">
        <BaseButton size="sm" variant="primary" :disabled="busy || !edited.trim()" @click="submitPartial">保存修正</BaseButton>
        <BaseButton size="sm" variant="text" :disabled="busy" @click="editing = false">取消</BaseButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.zj-chip {
  max-width: 760px;
  margin-top: 8px;
  padding: 10px 14px;
  border: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
}
.zj-chip__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-chip__lead {
  font-family: var(--ws-font-display, serif);
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}
.zj-chip__content {
  margin: 6px 0 2px;
  font-size: 15px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-chip__quote {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-chip__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.zj-chip__btn {
  padding: 4px 14px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.zj-chip__btn:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-chip__btn.is-primary {
  border-color: var(--ws-primary-color, #a6452e);
  background: var(--ws-primary-color, #a6452e);
  color: var(--ws-white, #fff);
}
.zj-chip__btn.is-danger {
  color: var(--ws-danger-color, #a6452e);
}
.zj-chip__btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.zj-chip__edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}
.zj-chip__edit-label {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-chip__textarea {
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
.zj-chip__textarea:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #a6452e);
}
.zj-chip__edit-actions {
  display: flex;
  gap: 8px;
}
</style>
