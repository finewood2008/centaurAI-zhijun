// 回复的节奏：纯渲染层。底层流式不变，这里只决定「现在给用户看到哪里」。
// 单位 = 段落（按换行切）；第一段再切出第一句；围栏代码块整块是一个单位。
// 只揭示边界已到的单位；段间停顿 clamp(300 + 1.2ms × 字数, 300, 600)；
// 流结束后按 90ms/单位追赶、总计封顶 900ms 再全量；aborted / error 立即全量。
// 减少动效、页面不可见、偏好关闭、挂载时已不在流式：直通。
// schedule / now 可注入，便于单测；卸载与消息 id 变化时清理定时器。
import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { pacedReplyEnabled } from '../pacedReplyPreference.ts'

export interface PacedUnit {
  text: string
  /** 边界已到：段尾换行、首句标点或代码块闭合；流结束时全部为 true */
  done: boolean
}

const FIRST_SENTENCE = /[。！？!?；;]/
const FENCE = /^(`{3,}|~{3,})/

/** 把正文切成揭示单位；complete 为 true 时最后一个单位也算已到边界。 */
export function splitPacedUnits(content: string, complete: boolean): PacedUnit[] {
  const units: PacedUnit[] = []
  const lines = content.split('\n')
  let i = 0
  let paragraphIndex = 0
  const consumeNewlines = (): string => {
    let run = ''
    while (i < lines.length - 1 && lines[i] === '') {
      run += '\n'
      i++
    }
    return run
  }
  while (i < lines.length) {
    const line = lines[i]
    const fence = line.match(FENCE)
    if (fence) {
      const marker = fence[1]
      let text = line
      let closed = false
      i++
      while (i < lines.length) {
        const next = lines[i]
        text += '\n' + next
        i++
        if (next.startsWith(marker) && next.trim() === marker) { closed = true; break }
      }
      if (closed) {
        if (i < lines.length) text += '\n' + consumeNewlines()
        units.push({ text, done: true })
      } else {
        units.push({ text, done: complete })
      }
      paragraphIndex++
      continue
    }
    if (line === '' && i < lines.length - 1) {
      // 段与段之间的空行归到前一个单位；开头的空行单独成一个已完成的单位
      const run = consumeNewlines()
      if (units.length) units[units.length - 1].text += run
      else units.push({ text: run, done: true })
      continue
    }
    if (line === '') { i++; continue }
    const last = i === lines.length - 1
    let text = line
    let done = true
    i++
    if (last) done = complete
    else text += '\n' + consumeNewlines()
    if (paragraphIndex === 0) {
      const match = FIRST_SENTENCE.exec(line)
      if (match && match.index < line.length - 1) {
        const head = line.slice(0, match.index + 1)
        units.push({ text: head, done: true })
        units.push({ text: text.slice(head.length), done })
        paragraphIndex++
        continue
      }
      if (match && match.index === line.length - 1 && last && !complete) done = true
    }
    units.push({ text, done })
    paragraphIndex++
  }
  return units
}

export function pausePlan(text: string): number {
  return Math.min(600, Math.max(300, 300 + 1.2 * text.replace(/\s+/g, '').length))
}

export type PacedStatus = 'complete' | 'aborted' | 'error' | null | undefined

export interface PacerOptions {
  schedule?: (fn: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
  now?: () => number
  onChange?: (revealed: string) => void
  /** 直通：减少动效、不可见、偏好关闭、历史消息 */
  passthrough?: boolean
  /** 首次 update 时把已到边界的单位一次揭示（重新挂载时不闪） */
  seed?: boolean
}

export interface Pacer {
  update(content: string, streaming: boolean, status?: PacedStatus): void
  flush(): void
  dispose(): void
  readonly revealed: string
  /** 还有没揭示的单位 */
  readonly pending: boolean
  readonly passthrough: boolean
}

const CATCHUP_STEP = 90
const CATCHUP_CAP = 900

export function createPacer(options: PacerOptions = {}): Pacer {
  const schedule = options.schedule ?? ((fn, ms) => setTimeout(fn, ms))
  const cancel = options.cancel ?? (handle => clearTimeout(handle as ReturnType<typeof setTimeout>))
  const now = options.now ?? (() => Date.now())
  let content = ''
  let units: PacedUnit[] = []
  let shown = 0
  let revealed = ''
  let timer: unknown = null
  let mode: 'live' | 'catchup' | 'flushed' = options.passthrough ? 'flushed' : 'live'
  let lastRevealAt = Number.NEGATIVE_INFINITY
  let catchupStart = 0
  let seeded = !options.seed
  let disposed = false

  const clearTimer = () => {
    if (timer !== null) cancel(timer)
    timer = null
  }
  const emit = () => {
    const next = mode === 'flushed' ? content : units.slice(0, shown).map(u => u.text).join('')
    if (next === revealed) return
    revealed = next
    options.onChange?.(revealed)
  }
  const available = () => {
    let n = 0
    while (n < units.length && units[n].done) n++
    return n
  }
  const revealOne = () => {
    shown = Math.min(units.length, shown + 1)
    lastRevealAt = now()
    emit()
  }
  const flush = () => {
    if (disposed) return
    clearTimer()
    mode = 'flushed'
    shown = units.length
    emit()
  }
  const liveTick = () => {
    timer = null
    if (disposed || mode !== 'live') return
    if (shown < available()) revealOne()
    scheduleLive()
  }
  function scheduleLive() {
    if (timer !== null || mode !== 'live') return
    const ready = available()
    if (shown >= ready) return
    if (shown === 0) {
      revealOne()
      if (shown >= available()) return
    }
    const wait = Math.max(0, lastRevealAt + pausePlan(units[shown - 1]?.text ?? '') - now())
    timer = schedule(liveTick, wait)
  }
  const catchupTick = () => {
    timer = null
    if (disposed || mode !== 'catchup') return
    if (now() - catchupStart >= CATCHUP_CAP) { flush(); return }
    revealOne()
    scheduleCatchup()
  }
  function scheduleCatchup() {
    if (timer !== null || mode !== 'catchup') return
    const remaining = units.length - shown
    if (remaining <= 0) { flush(); return }
    const elapsed = now() - catchupStart
    const budget = CATCHUP_CAP - elapsed
    if (budget <= 0) { flush(); return }
    const step = Math.max(0, Math.min(CATCHUP_STEP, Math.floor(budget / remaining)))
    timer = schedule(catchupTick, step)
  }

  return {
    update(nextContent, streaming, status) {
      if (disposed) return
      content = nextContent
      if (mode === 'flushed') { emit(); return }
      units = splitPacedUnits(content, !streaming)
      if (shown > units.length) shown = units.length
      if (!seeded) {
        seeded = true
        if (streaming && content) {
          shown = available()
          lastRevealAt = now()
          emit()
        }
      }
      if (!streaming && (status === 'aborted' || status === 'error')) { flush(); return }
      if (!streaming) {
        if (mode === 'live') {
          mode = 'catchup'
          catchupStart = now()
          clearTimer()
        }
        emit()
        scheduleCatchup()
        return
      }
      emit()
      scheduleLive()
    },
    flush,
    dispose() {
      disposed = true
      clearTimer()
    },
    get revealed() { return revealed },
    get pending() { return mode !== 'flushed' && shown < units.length },
    get passthrough() { return mode === 'flushed' },
  }
}

export interface PassthroughInput {
  enabled: boolean
  reducedMotion: boolean
  hidden: boolean
  streamingAtMount: boolean
}

export function shouldPassthrough(input: PassthroughInput): boolean {
  return !input.enabled || input.reducedMotion || input.hidden || !input.streamingAtMount
}

function prefersReducedMotion(): boolean {
  try { return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches } catch { return false }
}
function documentHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden === true
}

export interface PacedSource {
  id: string
  content: string
  streaming?: boolean
  status?: PacedStatus
}

/** 组件内使用：paced 为 false 时原样透传；否则按消息 id 各起一个节奏器。 */
export function usePacedReply(source: () => PacedSource, paced: () => boolean): { revealed: Ref<string>; pending: Ref<boolean> } {
  const revealed = ref(source().content)
  const pending = ref(false)
  let pacer: Pacer | null = null

  const stop = () => {
    pacer?.dispose()
    pacer = null
    pending.value = false
  }
  const start = () => {
    stop()
    const s = source()
    if (!paced()) { revealed.value = s.content; return }
    const passthrough = shouldPassthrough({
      enabled: pacedReplyEnabled.value,
      reducedMotion: prefersReducedMotion(),
      hidden: documentHidden(),
      streamingAtMount: !!s.streaming,
    })
    pacer = createPacer({
      passthrough,
      seed: true,
      onChange: text => { revealed.value = text },
    })
    pacer.update(s.content, !!s.streaming, s.status)
    revealed.value = pacer.revealed
    pending.value = pacer.pending
  }

  const key = computed(() => `${source().id}|${paced() ? 1 : 0}`)
  watch(key, start, { immediate: true })
  watch(() => [source().content, !!source().streaming, source().status] as const, ([content, streaming, status]) => {
    if (!pacer) { revealed.value = content; return }
    pacer.update(content, streaming, status)
    revealed.value = pacer.revealed
    pending.value = pacer.pending
  })

  const onVisibility = () => { if (documentHidden() && pacer) { pacer.flush(); revealed.value = pacer.revealed; pending.value = false } }
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility)
  onBeforeUnmount(() => {
    stop()
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility)
  })
  return { revealed, pending }
}
