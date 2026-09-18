<script setup lang="ts">
// 只读的历史块：进入视口前 800px 才读消息（同时最多 4 块在读）；占位只有时间、标题与一行产出；
// 跨午夜在消息之间插一道细分隔；块尾一枚「留」印（点开才见这段对话留下的）与「接着说」。
// 这里没有轮次逻辑：消息用 StreamTurn 原样渲染，不分段浮现。
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { getConversation, getConversationOutcomes, type Conversation, type ConversationOutcomes } from '@/services/api'
import OutcomesCard from '@/components/conversation/OutcomesCard.vue'
import StreamTurn from './StreamTurn.vue'
import MicroSeal from './MicroSeal.vue'
import { clockLabel, dayKey, dayLabel, hasOutcomesBrief, outcomesLine } from './dayStream'
import type { StreamMessage } from './streamTypes'

const MAX_CONCURRENT = 4
let running = 0
const waiting: Array<() => void> = []
function acquire(): Promise<void> {
  if (running < MAX_CONCURRENT) { running++; return Promise.resolve() }
  return new Promise(resolve => waiting.push(() => { running++; resolve() }))
}
function release() {
  running = Math.max(0, running - 1)
  const next = waiting.shift()
  if (next) next()
}

const props = withDefaults(defineProps<{ conversation: Conversation; highlighted?: boolean; now?: Date }>(), { highlighted: false, now: () => new Date() })

const router = useRouter()
const root = ref<HTMLElement | null>(null)
const messages = shallowRef<StreamMessage[] | null>(null)
const loadError = ref('')
const loadingMessages = ref(false)
const outcomes = ref<ConversationOutcomes | null>(null)
const outcomesOpen = ref(false)
const outcomesBusy = ref(false)
const outcomesError = ref('')
let observer: IntersectionObserver | null = null
let requested = false
let alive = true

const title = computed(() => props.conversation.title || (props.conversation.mode === 'review' ? '一次回访' : '一段对话'))
const time = computed(() => clockLabel(props.conversation.createdAt))
const line = computed(() => outcomesLine(props.conversation.outcomes))
const hasOutcomes = computed(() => hasOutcomesBrief(props.conversation.outcomes))

type Row = { kind: 'divider'; key: string; label: string } | { kind: 'message'; key: string; message: StreamMessage }
const keyOf = (iso: string | null | undefined): string => {
  const t = iso ? new Date(iso) : null
  return t && !Number.isNaN(t.valueOf()) ? dayKey(t) : ''
}
const rows = computed<Row[]>(() => {
  const out: Row[] = []
  let lastKey = keyOf(props.conversation.createdAt)
  for (const message of messages.value ?? []) {
    const key = keyOf(message.createdAt) || lastKey
    if (lastKey && key !== lastKey) out.push({ kind: 'divider', key: `divider:${message.id}`, label: dayLabel(new Date(message.createdAt), props.now) })
    lastKey = key
    out.push({ kind: 'message', key: message.id, message })
  }
  return out
})

async function load() {
  if (requested) return
  requested = true
  loadingMessages.value = true
  loadError.value = ''
  await acquire()
  try {
    if (!alive) return
    const detail = await getConversation(props.conversation.id)
    if (!alive) return
    messages.value = detail.messages.map(message => ({ ...message }))
  } catch (err) {
    if (!alive) return
    loadError.value = err instanceof Error ? err.message : '暂时读不到这段对话'
    requested = false
  } finally {
    release()
    if (alive) loadingMessages.value = false
  }
}

async function loadOutcomes() {
  if (outcomesBusy.value) return
  outcomesBusy.value = true
  outcomesError.value = ''
  try {
    outcomes.value = await getConversationOutcomes(props.conversation.id)
  } catch (err) {
    if (!alive) return
    outcomesError.value = err instanceof Error ? err.message : '暂时读不到这段对话留下的'
  } finally {
    if (alive) outcomesBusy.value = false
  }
}

function toggleOutcomes() {
  outcomesOpen.value = !outcomesOpen.value
  if (outcomesOpen.value && !outcomes.value) void loadOutcomes()
}

function continueHere() {
  void router.push(`/c/${encodeURIComponent(props.conversation.id)}`)
}

onMounted(() => {
  if (typeof IntersectionObserver === 'undefined' || !root.value) { void load(); return }
  observer = new IntersectionObserver(entries => {
    if (!entries.some(entry => entry.isIntersecting)) return
    observer?.disconnect()
    observer = null
    void load()
  }, { rootMargin: '800px 0px' })
  observer.observe(root.value)
})

onBeforeUnmount(() => {
  alive = false
  observer?.disconnect()
  observer = null
})
</script>

<template>
  <section
    ref="root"
    class="zj-block"
    :class="{ 'is-revealed': highlighted, 'is-loaded': !!messages }"
    :data-conversation-id="conversation.id"
    :aria-label="title"
  >
    <header class="zj-block__head">
      <span class="zj-block__time">{{ time }}</span>
      <h3 class="zj-block__title">{{ title }}</h3>
      <span v-if="line && !messages" class="zj-block__line">{{ line }}</span>
    </header>
    <div v-if="messages" class="zj-block__messages">
      <template v-for="row in rows" :key="row.key">
        <div v-if="row.kind === 'divider'" class="zj-block__midnight" role="separator" :aria-label="row.label"><span>{{ row.label }}</span></div>
        <StreamTurn v-else :message="row.message" :conversation-id="conversation.id" />
      </template>
    </div>
    <div v-else class="zj-block__placeholder" :aria-busy="loadingMessages">
      <p v-if="loadError" class="zj-block__note">{{ loadError }} <button type="button" @click="load">再读一次</button></p>
      <p v-else class="zj-block__note">{{ conversation.messageCount }} 条消息</p>
    </div>
    <footer class="zj-block__foot">
      <MicroSeal v-if="hasOutcomes" kind="留" label="这段对话留下的" :pressed="outcomesOpen" @toggle="toggleOutcomes" />
      <button type="button" class="zj-block__continue" @click="continueHere">接着说</button>
    </footer>
    <p v-if="outcomesOpen && outcomesBusy && !outcomes" class="zj-block__note" role="status">在找这段对话留下的…</p>
    <p v-if="outcomesOpen && outcomesError" class="zj-block__note">{{ outcomesError }} <button type="button" @click="loadOutcomes">再读一次</button></p>
    <OutcomesCard v-if="outcomesOpen && outcomes" :outcomes="outcomes" :conversation-id="conversation.id" @refresh="loadOutcomes" />
  </section>
</template>
