<script setup lang="ts">
// AI 问答：回答 / 证据 / 证据不足三层信息（B3 FE-UI-016）
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Send, SearchX, CheckCircle2 } from 'lucide-vue-next'
import { api, type QaResponse, type QaCitation, type CorrectionNotice } from '@/services/api'
import { sourceRoute, correctionDetailRoute } from '@/shared/routes'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'

const router = useRouter()

const question = ref('')
const submitting = ref(false)
const result = ref<QaResponse | null>(null)
const error = ref('')
const validationError = ref('')
let lastQuestion = ''

async function submit() {
  const q = question.value.trim()
  validationError.value = ''
  if (q.length < 2 || q.length > 500) {
    validationError.value = '请输入 2 到 500 字的问题'
    return
  }
  if (submitting.value) return

  submitting.value = true
  error.value = ''
  result.value = null
  lastQuestion = q

  try {
    result.value = await api.askQuestion(q)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '问答暂时失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}

function retry() {
  if (lastQuestion) {
    question.value = lastQuestion
    submit()
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}

function navigateToCitation(citation: QaCitation) {
  if (citation.sourceType === 'material' && citation.materialId) {
    router.push(`/materials/${citation.materialId}`)
  }
  if (citation.sourceType === 'knowledge' && citation.knowledgeId) {
    router.push(`/knowledge/${citation.knowledgeId}`)
  }
}

// P14-12：纠错来源跳转（material → /materials/{id}，knowledge → /knowledge/{id}）
function openCorrectionSource(sourceId: string) {
  router.push(sourceRoute(sourceId))
}

// P14-12：打开具体纠错记录（跳转纠错本并定位）
function goToCorrection(notice: CorrectionNotice) {
  router.push(correctionDetailRoute(notice.correctionId))
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1>问知君</h1>
      <p>基于知识档案和原材料回答；资料不足时不生成结论。默认本地模型，外发仅在显式开启时进行。</p>
    </div>

    <!-- 问题输入 -->
    <div class="ws-qa-input">
      <textarea
        v-model="question"
        class="ws-qa-input__field"
        placeholder="输入你的问题…"
        rows="2"
        :disabled="submitting"
        @keydown="onKeydown"
      />
      <BaseButton
        variant="primary"
        :loading="submitting"
        :disabled="submitting || !question.trim()"
        @click="submit"
      >
        <Send :size="15" aria-hidden="true" />{{ submitting ? '正在检索…' : '提问' }}
      </BaseButton>
    </div>
    <p v-if="validationError" class="ws-qa-error">{{ validationError }}</p>

    <div v-if="submitting" class="loading-state">正在检索证据并生成回答…</div>
    <ErrorState v-else-if="error" :message="error" retry-label="重试" @retry="retry" />

    <template v-else-if="result">
      <!-- P14-12：纠错提醒（问题或证据命中已纠正观点时置顶展示；提醒非模型自由文本） -->
      <div v-if="result.correctionNotices.length" class="ws-qa-correction">
        <div class="ws-qa-correction__head">
          <CheckCircle2 :size="16" aria-hidden="true" />
          <strong>此观点已被修正</strong>
        </div>
        <div v-for="notice in result.correctionNotices" :key="notice.correctionId" class="ws-qa-correction__item">
          <span class="ws-qa-correction__body">
            <span class="ws-qa-correction__title">{{ notice.title }}</span>
            <span class="ws-qa-correction__claim">{{ notice.correctedClaim }}</span>
          </span>
          <span class="ws-qa-correction__actions">
            <button type="button" class="ws-qa-correction__source" @click="goToCorrection(notice)">查看纠错记录</button>
            <button
              v-for="sid in notice.sourceIds"
              :key="sid"
              type="button"
              class="ws-qa-correction__source"
              @click="openCorrectionSource(sid)"
            >查看来源</button>
          </span>
        </div>
      </div>

      <!-- 证据不足：明确提示，不作为结论展示 -->
      <div v-if="result.status === 'INSUFFICIENT_EVIDENCE'" class="ws-qa-insufficient">
        <SearchX :size="20" aria-hidden="true" />
        <div>
          <strong>证据不足，暂不生成结论</strong>
          <p>本地资料中未能找到足够支撑依据，以下仅为可用的相关线索。</p>
        </div>
      </div>

      <!-- 已检索到证据但模型无法生成完整结论：保留资料范围和可打开引用，不能误称为无证据。 -->
      <div v-if="result.status === 'PARTIAL_ANSWER'" class="ws-qa-insufficient is-partial">
        <SearchX :size="20" aria-hidden="true" />
        <div>
          <strong>已找到相关资料，暂未生成完整结论</strong>
          <p>{{ result.answer }}</p>
        </div>
      </div>

      <!-- 回答区 -->
      <div v-if="result.status === 'ANSWERED'" class="ws-qa-answer">
        <div class="ws-qa-answer__head">
          <span class="ws-qa-answer__badge"><CheckCircle2 :size="14" aria-hidden="true" />基于资料的回答</span>
        </div>
        <div class="ws-qa-question">{{ result.question }}</div>
        <div class="ws-qa-answer__text">{{ result.answer }}</div>
      </div>

      <!-- 双通道提示：外部生成 / 本地生成 / 外部失败回落本地（§9 隐私要求展示当前通道与外发说明） -->
      <div
        v-if="result.meta.provider"
        class="ws-qa-channel"
        :class="result.meta.fallbackUsed ? 'is-fallback' : result.meta.provider === 'openai' ? 'is-external' : 'is-local'"
      >
        <template v-if="result.meta.provider === 'openai' && !result.meta.fallbackUsed">
          本次回答由外部模型「{{ result.meta.model }}」生成，问题与证据片段已发送至外部服务。
        </template>
        <template v-else-if="result.meta.fallbackUsed">
          外部模型不可用，已切换本地模型「{{ result.meta.model }}」生成。
        </template>
        <template v-else>
          本次回答由本地模型「{{ result.meta.model }}」生成，数据未离开本机。
        </template>
      </div>

      <!-- 证据区 -->
      <div v-if="result.citations.length" class="ws-qa-evidence">
        <h2>证据来源（{{ result.citations.length }}）</h2>
        <div class="ws-qa-evidence__list">
          <button
            v-for="citation in result.citations"
            :key="citation.citationId"
            class="ws-qa-evidence__item"
            type="button"
            @click="navigateToCitation(citation)"
          >
            <span
              class="ws-qa-evidence__kind"
              :class="citation.sourceType === 'knowledge' ? 'is-knowledge' : 'is-material'"
            >
              {{ citation.sourceType === 'knowledge' ? '知识卡片' : '原材料' }}
            </span>
            <span class="ws-qa-evidence__main">
              <strong>{{ citation.title }}</strong>
              <span class="ws-qa-evidence__snippet">{{ citation.snippet }}</span>
            </span>
          </button>
        </div>
      </div>
    </template>

    <EmptyState
      v-else
      title="输入问题开始问答"
      description="系统将从知识档案和原材料中检索证据并生成回答。"
    />
  </div>
</template>

<style scoped>
.ws-qa-input {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  max-width: 760px;
  margin-bottom: 6px;
}

.ws-qa-input__field {
  flex: 1;
  resize: vertical;
  min-height: 44px;
  max-height: 200px;
  padding: 10px 14px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  color: var(--ws-text-primary-color, #303133);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  transition: border-color 0.15s;
}
.ws-qa-input__field:focus {
  outline: none;
  border-color: var(--ws-input-focus-border-color, #1b99ff);
}
.ws-qa-input__field:disabled {
  opacity: 0.55;
}

.ws-qa-error {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--ws-danger-color, #ff4918);
}

/* P14-12：纠错提醒（置顶风险提示，独立于回答渲染） */
.ws-qa-correction {
  max-width: 760px;
  margin: 16px 0;
  padding: 12px 16px;
  border: 1px solid var(--ws-danger-color, #ff4918);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-danger-color-bd, rgba(255, 73, 24, 0.06));
}
.ws-qa-correction__head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  color: var(--ws-danger-color, #ff4918);
  font-size: 13px;
}
.ws-qa-correction__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  border-top: 1px dashed var(--ws-danger-color, rgba(255, 73, 24, 0.35));
}
.ws-qa-correction__item:first-of-type {
  border-top: none;
  padding-top: 0;
}
.ws-qa-correction__body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ws-qa-correction__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-danger-color, #ff4918);
}
.ws-qa-correction__claim {
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #303133);
}
.ws-qa-correction__actions {
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}
.ws-qa-correction__source {
  padding: 3px 10px;
  border: 1px solid var(--ws-danger-color, #ff4918);
  border-radius: 999px;
  background: transparent;
  color: var(--ws-danger-color, #ff4918);
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s;
}
.ws-qa-correction__source:hover {
  background: var(--ws-danger-color-bd, rgba(255, 73, 24, 0.1));
}

/* 证据不足 */
.ws-qa-insufficient {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  max-width: 760px;
  margin: 16px 0;
  padding: 16px 18px;
  border: 1px solid var(--ws-warning-color, #ffbc3d);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-warning-color-bd, rgba(255, 188, 61, 0.08));
  color: #b45309;
}
.ws-qa-insufficient strong {
  font-size: 14px;
}
.ws-qa-insufficient p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
}

/* 双通道提示（§9：展示当前通道与外发说明；fallback 时明确告知已回落本地） */
.ws-qa-channel {
  max-width: 760px;
  margin: 0 0 14px;
  padding: 8px 14px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-left-width: 3px;
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #909399);
}
.ws-qa-channel.is-local {
  border-left-color: var(--ws-success-color, #12cd3d);
}
.ws-qa-channel.is-external {
  border-left-color: var(--ws-warning-color, #ffbc3d);
}
.ws-qa-channel.is-fallback {
  border-left-color: var(--ws-warning-color, #ffbc3d);
}

/* 回答区 */
.ws-qa-answer {
  max-width: 760px;
  margin: 16px 0;
  padding: 16px 20px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
}

.ws-qa-answer__head {
  margin-bottom: 10px;
}

.ws-qa-answer__badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--ws-success-color-bd, rgba(18, 205, 61, 0.06));
  color: var(--ws-success-color, #12cd3d);
  font-size: 12px;
  font-weight: 600;
}

.ws-qa-question {
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--ws-text-secondary-color, #909399);
}

.ws-qa-answer__text {
  font-size: 15px;
  line-height: 1.7;
  color: var(--ws-text-primary-color, #303133);
  white-space: pre-wrap;
}

/* 证据区 */
.ws-qa-evidence {
  max-width: 760px;
}
.ws-qa-evidence h2 {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--ws-text-primary-color, #303133);
}

.ws-qa-evidence__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ws-qa-evidence__item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  width: 100%;
  padding: 12px 16px;
  border: 1px solid var(--ws-border-color, #dcdfe6);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fff);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.ws-qa-evidence__item:hover {
  border-color: var(--ws-primary-color, #0077ff);
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.04));
}

.ws-qa-evidence__kind {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}
.ws-qa-evidence__kind.is-knowledge {
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.06));
  color: var(--ws-main-color, #1b99ff);
}
.ws-qa-evidence__kind.is-material {
  background: var(--ws-success-color-bd, rgba(18, 205, 61, 0.06));
  color: var(--ws-success-color, #12cd3d);
}

.ws-qa-evidence__main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.ws-qa-evidence__main strong {
  font-size: 14px;
  color: var(--ws-text-primary-color, #303133);
}
.ws-qa-evidence__snippet {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-color, #606266);
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 600px) {
  .ws-qa-input {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
