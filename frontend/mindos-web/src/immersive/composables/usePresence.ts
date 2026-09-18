// 在场一行：收集输入（当前轮桥接、准备阶段、输入框焦点、当天首次、小时），交给 presenceLine 出措辞。
// 桌面连接状态与断连由调用方以 getter 传入，这里不引入 src/desktop/*。
import { computed, getCurrentInstance, onBeforeUnmount, onMounted, ref, watch, type ComputedRef, type Ref } from 'vue'
import { chatPreparation } from '@/services/taskRouting'
import { getZhijunHome } from '@/services/api'
import { modelUnavailable } from '@/shared/model'
import { useActiveTurn } from '../activeTurn'
import { COMPOSER_HOST } from '../shellContext'
import { dayKeyOf, isFirstOpenToday, presenceLine, type PresenceLine } from './presenceLine'

export const PRESENCE_DAY_KEY = 'zhijun.presence.day'
const HOUR_TICK_MS = 10 * 60 * 1000

export interface UsePresenceOptions {
  workspaceReady: () => boolean
  connectionLabel: () => string
  backendDown: () => boolean
  now?: () => Date
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  /** 认识的天数；默认读 /mindos/zhijun/home 的 map.relationshipDays，只在当天首次打开时读一次。 */
  loadRelationshipDays?: () => Promise<number | null>
}

export interface PresenceModel {
  presence: ComputedRef<PresenceLine>
  composerFocused: Readonly<Ref<boolean>>
  firstToday: Readonly<Ref<boolean>>
  relationshipDays: Readonly<Ref<number | null>>
}

function defaultStorage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

async function defaultRelationshipDays(): Promise<number | null> {
  try {
    const home = await getZhijunHome()
    const days = home?.map?.relationshipDays
    return typeof days === 'number' && days > 0 ? days : null
  } catch {
    return null
  }
}

export function usePresence(options: UsePresenceOptions): PresenceModel {
  const now = options.now ?? (() => new Date())
  const storage = options.storage === undefined ? defaultStorage() : options.storage
  const loadDays = options.loadRelationshipDays ?? defaultRelationshipDays
  const turn = useActiveTurn()
  const hour = ref(now().getHours())
  const composerFocused = ref(false)
  const firstToday = ref(false)
  const relationshipDays = ref<number | null>(null)
  let daysRequested = false

  function inComposer(target: EventTarget | null): boolean {
    return target instanceof Element && !!target.closest(COMPOSER_HOST)
  }
  const onFocusIn = (event: FocusEvent) => { if (inComposer(event.target)) composerFocused.value = true }
  const onFocusOut = (event: FocusEvent) => {
    if (!inComposer(event.target)) return
    if (!inComposer(event.relatedTarget)) composerFocused.value = false
  }

  function requestDays() {
    if (daysRequested || !firstToday.value || !options.workspaceReady()) return
    daysRequested = true
    void loadDays().then(days => { relationshipDays.value = days })
  }

  const presence = computed<PresenceLine>(() => {
    const bridge = turn.value
    const status = bridge?.status.value ?? null
    return presenceLine({
      workspaceReady: options.workspaceReady(),
      connectionLabel: options.connectionLabel(),
      backendDown: options.backendDown(),
      modelUnavailable: modelUnavailable(status),
      streaming: !!bridge?.streaming.value,
      preparationStage: chatPreparation.value?.stage ?? null,
      pendingJobs: status?.pendingJobs ?? 0,
      composerFocused: composerFocused.value,
      firstToday: firstToday.value,
      relationshipDays: relationshipDays.value,
      hour: hour.value,
    })
  })

  if (getCurrentInstance()) {
    let timer: ReturnType<typeof setInterval> | undefined
    onMounted(() => {
      const today = dayKeyOf(now())
      let stored: string | null = null
      try { stored = storage?.getItem(PRESENCE_DAY_KEY) ?? null } catch { stored = null }
      firstToday.value = isFirstOpenToday(stored, today)
      if (firstToday.value) { try { storage?.setItem(PRESENCE_DAY_KEY, today) } catch { /* 无法持久化时明天会再说一次 */ } }
      requestDays()
      timer = setInterval(() => { hour.value = now().getHours() }, HOUR_TICK_MS)
      document.addEventListener('focusin', onFocusIn)
      document.addEventListener('focusout', onFocusOut)
    })
    onBeforeUnmount(() => {
      clearInterval(timer)
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('focusout', onFocusOut)
    })
    // 桌面端连上之后才去读天数；第一轮对话开始后，「第 N 天」让位给其它状态
    watch(() => options.workspaceReady(), ready => { if (ready) requestDays() })
    watch(() => !!turn.value?.streaming.value, streaming => { if (streaming) firstToday.value = false })
  }

  return { presence, composerFocused, firstToday, relationshipDays }
}
