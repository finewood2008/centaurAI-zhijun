// 一条流的数据：分页读会话列表、按天分节、今天的信、当前块的判定与「回到某一天」。
// 只读列表与来信；轮次逻辑仍在嵌入的 ConversationPage，这里经 activeTurn 桥接只读它的状态。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch, type ShallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getConversation, getZhijunHome, listConversations, type Conversation, type HomeBrief, type HomeNextAction, type ZhijunHomeOverview } from '@/services/api'
import { useActiveTurn } from '../activeTurn'
import { dayKey, dayOf, groupByDay, mergePage, pickActiveConversation, withDay, type DayGroup } from '../dayStream'

const PAGE_SIZE = 30
const REVEAL_EXTRA_PAGES = 5
const HIGHLIGHT_MS = 8000
const LETTER_POLL_MS = 1500
const LETTER_POLL_MAX = 7

export interface DayLetter {
  brief: HomeBrief
  nextAction: HomeNextAction | null
}

/** 给抽屉等其它部件用的两个动作；由挂载中的 DayStream 登记。 */
export interface DayStreamBridge {
  revealConversation(id: string): Promise<boolean>
  refreshList(): Promise<void>
}

const dayStreamBridge = shallowRef<DayStreamBridge | null>(null)

export function useDayStreamBridge(): Readonly<ShallowRef<DayStreamBridge | null>> {
  return dayStreamBridge
}

export function useDayStream(scroller: () => HTMLElement | null) {
  const route = useRoute()
  const router = useRouter()
  const activeTurn = useActiveTurn()

  const items = ref<Conversation[]>([])
  const pagedCount = ref(0)
  const total = ref(0)
  const loading = ref(true)
  const loadingOlder = ref(false)
  const error = ref('')
  const home = ref<ZhijunHomeOverview | null>(null)
  const letterRefreshing = ref(false)
  /** 列表里找不到、单独挂在顶部「更早的日子」的那一块 */
  const lonely = ref<Conversation | null>(null)
  const highlightedId = ref<string | null>(null)
  const now = ref(new Date())

  let alive = true
  let letterTimer: ReturnType<typeof setTimeout> | null = null
  let letterPolls = 0
  let highlightTimer: ReturnType<typeof setTimeout> | null = null
  let clockTimer: ReturnType<typeof setInterval> | null = null
  let pendingSay: string | null = null

  const routeId = computed(() => (route.path.startsWith('/c/') && typeof route.params.conversationId === 'string' ? route.params.conversationId : null))
  const picked = computed(() => pickActiveConversation(items.value, now.value, routeId.value))
  const activeId = computed(() => routeId.value ?? picked.value?.id ?? null)
  const activeConversation = computed<Conversation | null>(() => {
    const bridged = activeTurn.value?.current.value
    if (bridged && bridged.id === activeId.value) return bridged
    return items.value.find(item => item.id === activeId.value) ?? bridged ?? null
  })
  const streaming = computed(() => !!activeTurn.value?.streaming.value)
  const history = computed(() => items.value.filter(item => item.id !== activeId.value && item.id !== lonely.value?.id))
  const todayKey = computed(() => dayKey(now.value))
  const activeDayKey = computed(() => (activeConversation.value ? dayKey(dayOf(activeConversation.value, now.value)) : todayKey.value))
  const days = computed<DayGroup[]>(() => {
    let groups = groupByDay(history.value, now.value)
    groups = withDay(groups, now.value, now.value)
    if (activeConversation.value) groups = withDay(groups, dayOf(activeConversation.value, now.value), now.value)
    return groups
  })
  const letter = computed<DayLetter | null>(() => (home.value ? { brief: home.value.brief, nextAction: home.value.nextAction ?? null } : null))
  const hasMore = computed(() => pagedCount.value < total.value)
  /** 当前块不是今天的对话（回访、或另一天创建的会话）时，输入框上方提示在回复谁 */
  const replyingTo = computed<Conversation | null>(() => {
    const conv = activeConversation.value
    if (!conv) return null
    return activeDayKey.value !== todayKey.value || conv.mode !== 'chat' ? conv : null
  })
  const empty = computed(() => !loading.value && history.value.length === 0 && !lonely.value && !activeConversation.value && !streaming.value)

  async function fetchPage(offset: number) {
    return listConversations({ status: 'all', limit: PAGE_SIZE, offset })
  }

  async function loadFirst() {
    loading.value = true
    error.value = ''
    try {
      const page = await fetchPage(0)
      if (!alive) return
      items.value = mergePage([], page.items)
      pagedCount.value = page.items.length
      total.value = page.total
    } catch (err) {
      if (!alive) return
      error.value = err instanceof Error ? err.message : '暂时读不到过去的对话'
    } finally {
      if (alive) loading.value = false
    }
  }

  /** 往上翻：再读一页更早的；返回是否读到了新的会话。 */
  async function loadOlder(): Promise<boolean> {
    if (loadingOlder.value || loading.value || !hasMore.value) return false
    loadingOlder.value = true
    try {
      const page = await fetchPage(pagedCount.value)
      if (!alive) return false
      const before = items.value.length
      items.value = mergePage(items.value, page.items)
      pagedCount.value += page.items.length
      total.value = page.total
      if (!page.items.length) total.value = pagedCount.value
      return items.value.length > before
    } catch {
      return false
    } finally {
      if (alive) loadingOlder.value = false
    }
  }

  /** 一轮结束后重读第一页：块的产出摘要与新会话都在这一页。 */
  async function refreshList(): Promise<void> {
    now.value = new Date()
    try {
      const page = await fetchPage(0)
      if (!alive) return
      items.value = mergePage(items.value, page.items)
      total.value = page.total
      pagedCount.value = Math.max(pagedCount.value, page.items.length)
      if (lonely.value && items.value.some(item => item.id === lonely.value?.id)) lonely.value = null
    } catch {
      // 列表刷新失败不打断对话
    }
  }

  function clearLetterPoll() {
    if (letterTimer) clearTimeout(letterTimer)
    letterTimer = null
  }

  function scheduleLetterPoll() {
    clearLetterPoll()
    if (!alive || home.value?.brief.status !== 'refreshing' || letterPolls >= LETTER_POLL_MAX) {
      letterRefreshing.value = home.value?.brief.status === 'refreshing'
      return
    }
    letterRefreshing.value = true
    letterTimer = setTimeout(() => {
      letterPolls += 1
      void loadLetter()
    }, LETTER_POLL_MS)
  }

  async function loadLetter(): Promise<void> {
    try {
      const result = await getZhijunHome()
      if (!alive) return
      home.value = result
      scheduleLetterPoll()
    } catch {
      // 旧盒端没有来信；今天就从对话开始
      if (alive && home.value) scheduleLetterPoll()
    }
  }

  async function refreshLetter(): Promise<void> {
    letterPolls = 0
    await loadLetter()
  }

  function clearHighlight() {
    if (highlightTimer) clearTimeout(highlightTimer)
    highlightTimer = null
  }

  async function scrollToBlock(id: string): Promise<boolean> {
    await nextTick()
    const el = scroller()
    const target = el?.querySelector<HTMLElement>(`[data-conversation-id="${CSS.escape(id)}"]`)
    if (!el || !target) return false
    target.scrollIntoView({ block: 'start', behavior: 'smooth' })
    highlightedId.value = id
    clearHighlight()
    highlightTimer = setTimeout(() => {
      if (highlightedId.value === id) highlightedId.value = null
    }, HIGHLIGHT_MS)
    return true
  }

  const known = (id: string) => items.value.some(item => item.id === id) || lonely.value?.id === id

  /** 回到那一天：已加载则滚过去并高亮 8 秒；未加载最多再拉 5 页；仍无则顶部单独挂这一块。 */
  async function revealConversation(id: string): Promise<boolean> {
    if (known(id)) return scrollToBlock(id)
    for (let n = 0; n < REVEAL_EXTRA_PAGES && hasMore.value; n++) {
      await loadOlder()
      if (known(id)) return scrollToBlock(id)
    }
    try {
      const detail = await getConversation(id)
      if (!alive) return false
      lonely.value = detail.conversation
      return scrollToBlock(id)
    } catch {
      return false
    }
  }

  // /chat?say=… 在对话页消费后会被清掉；这里先记住，改写到今天的会话后再放回输入框
  watch(() => route.query.say, value => {
    if (typeof value === 'string' && value && route.path === '/chat') pendingSay = value
  }, { immediate: true })

  // /chat 且今天已有对话 → 直接进那个会话（新的一天才是新会话）
  watch([picked, () => route.path, loading], async ([conv, path, isLoading]) => {
    if (isLoading) return
    if (path !== '/chat') return
    if (!conv) { pendingSay = null; return }
    const say = pendingSay
    pendingSay = null
    await router.replace({ path: `/c/${encodeURIComponent(conv.id)}`, query: route.query })
    if (say) {
      await nextTick()
      activeTurn.value?.setText(say)
    }
  })

  // 一轮说完：列表里这一块的产出摘要该更新了；新建的会话也在这一页
  watch(() => activeTurn.value?.lastTurnAt.value ?? null, (value, previous) => {
    if (value && value !== previous && !loading.value) void refreshList()
  })
  watch(routeId, id => {
    if (id && !loading.value && !known(id)) void refreshList()
  })

  onMounted(() => {
    void loadFirst()
    void loadLetter()
    clockTimer = setInterval(() => {
      if (dayKey(new Date()) !== dayKey(now.value)) now.value = new Date()
    }, 60_000)
    dayStreamBridge.value = { revealConversation, refreshList }
  })

  onBeforeUnmount(() => {
    alive = false
    clearLetterPoll()
    clearHighlight()
    if (clockTimer) clearInterval(clockTimer)
    if (dayStreamBridge.value?.revealConversation === revealConversation) dayStreamBridge.value = null
  })

  return {
    items, days, letter, letterRefreshing, loading, loadingOlder, error, hasMore, lonely, highlightedId, now,
    activeId, activeConversation, activeDayKey, todayKey, streaming, replyingTo, empty,
    loadOlder, refreshList, refreshLetter, revealConversation,
  }
}

export type DayStreamState = ReturnType<typeof useDayStream>
