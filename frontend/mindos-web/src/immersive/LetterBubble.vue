<script setup lang="ts">
// 今日来信 = 当天第一条知君消息：宋体正文、一枚知印、一行极淡状态字（点开才见依据）、下面一句内联建议（nextAction）。
// 建议的处理与今日页一致：建档 / 继续建档 → 路由；确认 → 打开那条理解；回看 / 反思 → 起回访会话；
// 其余（问句 / 承诺 / 提醒 / 聊天）有当前块就直接放进输入框，否则带着话头去对话。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createConversation, updateOnboarding, type HomeBrief, type HomeNextAction, type HomeSourceRef } from '@/services/api'
import { useToast } from '@/composables/useToast'
import { useActiveTurn } from './activeTurn'
import SealMark from './SealMark.vue'

const props = defineProps<{ brief: HomeBrief; nextAction?: HomeNextAction | null; refreshing?: boolean }>()
const emit = defineEmits<{ (e: 'confirm', targetId: string | null): void; (e: 'source', source: HomeSourceRef): void }>()

const router = useRouter()
const toast = useToast()
const activeTurn = useActiveTurn()
const sourcesOpen = ref(false)
const busy = ref(false)

const refreshing = computed(() => props.refreshing || props.brief.status === 'refreshing')
const statusText = computed(() => (refreshing.value ? '我在重新整理' : `依据 ${props.brief.sourceRefs.length} 条 · 今日已送达`))
const paragraphs = computed(() => props.brief.message.split(/\n+/).map(s => s.trim()).filter(Boolean))
const suggestion = computed(() => props.nextAction?.title ?? '')
const hint = computed(() => (props.nextAction?.kind === 'inquiry' ? '读完这封信，我还想问你一句' : '读完这封信，可以从这里继续'))

function openSource(source: HomeSourceRef) {
  emit('source', source)
  if (source.sourceType === 'decision') {
    void router.push({ path: '/review', query: { decisionId: source.id.replace(/^decision:/, '') } })
    return
  }
  void router.push({ path: '/me', query: { claim: source.id.replace(/^claim:/, '') } })
}

async function runSuggestion() {
  const action = props.nextAction
  if (!action || busy.value) return
  if (action.kind === 'onboarding') {
    busy.value = true
    try {
      await updateOnboarding('restart')
      await router.push('/onboarding/chat')
    } catch (err) {
      toast({ type: 'error', message: err instanceof Error ? err.message : '暂时无法开始第一次认识' })
    } finally {
      busy.value = false
    }
    return
  }
  if (action.kind === 'resume_onboarding' && action.targetId) {
    void router.push(`/onboarding/c/${encodeURIComponent(action.targetId)}`)
    return
  }
  if (action.kind === 'confirm') {
    emit('confirm', action.targetId)
    void router.push(action.targetId ? { path: '/me', query: { claim: action.targetId } } : '/me/inbox')
    return
  }
  if ((action.kind === 'review' || action.kind === 'reflect') && action.targetId) {
    busy.value = true
    try {
      const conversation = await createConversation({ mode: 'review', decisionId: action.targetId })
      await router.push(`/c/${encodeURIComponent(conversation.id)}`)
    } catch (err) {
      toast({ type: 'error', message: err instanceof Error ? err.message : '无法开始回访' })
    } finally {
      busy.value = false
    }
    return
  }
  const turn = activeTurn.value
  if (turn && action.say) {
    turn.setText(action.say)
    turn.focus()
    return
  }
  void router.push({ path: '/chat', query: action.say ? { say: action.say, localOnly: '1' } : undefined })
}
</script>

<template>
  <article class="zj-letter-bubble" aria-label="知君写给你的今日来信" data-testid="letter-bubble">
    <div class="zj-letter-bubble__seal"><SealMark :breathing="refreshing" /></div>
    <div class="zj-letter-bubble__body">
      <p class="zj-letter-bubble__headline">{{ brief.headline }}</p>
      <p v-for="(text, index) in paragraphs" :key="index" class="zj-letter-bubble__text">{{ text }}</p>
      <div class="zj-letter-bubble__status">
        <button
          v-if="brief.sourceRefs.length"
          type="button"
          class="zj-letter-bubble__status-line zj-letter-bubble__status-line--button"
          data-testid="letter-status"
          :aria-expanded="sourcesOpen"
          :title="sourcesOpen ? '收起依据' : '看这封信的依据'"
          @click="sourcesOpen = !sourcesOpen"
        >{{ statusText }}</button>
        <span v-else class="zj-letter-bubble__status-line" data-testid="letter-status">{{ statusText }}</span>
      </div>
      <div v-if="sourcesOpen && brief.sourceRefs.length" class="zj-letter-bubble__sources" aria-label="这封来信的依据">
        <button
          v-for="source in brief.sourceRefs"
          :key="`${source.sourceType}:${source.id}`"
          type="button"
          @click="openSource(source)"
        ><span class="zj-seal zj-seal--muted">{{ source.label }}</span>{{ source.title }}</button>
      </div>
      <button v-if="suggestion" type="button" class="zj-letter-bubble__suggest" data-testid="letter-suggestion" :disabled="busy" @click="runSuggestion">
        <small>{{ hint }}</small>
        <strong>{{ suggestion }}</strong>
      </button>
    </div>
  </article>
</template>

<style scoped>
.zj-letter-bubble { display: flex; gap: 10px; margin: 0 0 22px; }
.zj-letter-bubble__seal { flex: none; padding-top: 10px; }
.zj-letter-bubble__body { min-width: 0; flex: 1; max-width: 760px; padding: 4px 0 4px 2px; }
.zj-letter-bubble__headline {
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  font-weight: 600;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-letter-bubble__text {
  margin: 0 0 10px;
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  line-height: 1.8;
  color: var(--ws-text-primary-color, #1d211f);
  white-space: pre-wrap;
}
.zj-letter-bubble__status { margin: 2px 0 0; }
.zj-letter-bubble__status-line {
  display: inline-block;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  text-align: left;
}
.zj-letter-bubble__status-line--button { cursor: pointer; border-bottom: 1px dotted transparent; }
.zj-letter-bubble__status-line--button:hover,
.zj-letter-bubble__status-line--button[aria-expanded='true'] { color: var(--ws-text-secondary-color, #686b66); border-bottom-color: currentColor; }
.zj-letter-bubble__sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; }
.zj-letter-bubble__sources button {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 14px;
  background: var(--ws-card-bg, #fff); color: var(--ws-text-secondary-color, #686b66);
  font: inherit; font-size: 13px; cursor: pointer;
}
.zj-letter-bubble__sources button:hover { color: var(--ws-text-primary-color, #1d211f); border-color: var(--ws-text-secondary-color, #686b66); }
.zj-letter-bubble__suggest {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  margin: 12px 0 0; padding: 8px 12px;
  border: 1px solid var(--ws-border-color-2, #e2ded4); border-left: 2px solid var(--ws-primary-color, #a6452e); border-radius: 4px;
  background: transparent; color: var(--ws-text-primary-color, #1d211f);
  font: inherit; text-align: left; cursor: pointer;
}
.zj-letter-bubble__suggest:hover:not(:disabled) { background: var(--ws-surface-2, #fbf8f1); }
.zj-letter-bubble__suggest:disabled { opacity: 0.6; cursor: default; }
.zj-letter-bubble__suggest small { font-size: 12px; color: var(--ws-text-placeholder-color, #a3a69f); }
.zj-letter-bubble__suggest strong { font-size: 14px; font-weight: 500; }
</style>
