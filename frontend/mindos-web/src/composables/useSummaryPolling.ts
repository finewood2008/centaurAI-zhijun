// 摘要轮询器（P14-03）。
//
// 使用不可复用的 session token 防“串台”：start/stop 都会递增 session，
// 每个 tick 捕获自己的 sessionId，写入结果前必须校验 sessionId 仍是当前 session，
// 否则丢弃。这样「资料 A 的在途请求延迟返回、用户已切到资料 B」时，A 的结果不会
// 覆盖 B 的摘要。
export type SummaryPollStatus = 'pending' | 'ok' | 'failed' | 'unavailable' | 'skipped'

export interface SummaryPollFetched {
  materialId: string
  text: string
  status: SummaryPollStatus
  generatedAt: string | null
}

export interface SummaryPollResult {
  text: string
  status: SummaryPollStatus
  generatedAt: string | null
}

export interface SummaryPollerOptions {
  // 单次轮询请求；失败时 reject（调用方可选择在下一轮重试）
  fetch: (materialId: string) => Promise<SummaryPollFetched>
  // 轮询到非 pending 终态时回调（调用方负责校验当前资料仍一致）
  onResult: (materialId: string, result: SummaryPollResult) => void
  // 等待预算耗尽仍未完成时回调
  onTimeout: (materialId: string) => void
  intervalMs?: number
  timeoutMs?: number
}

export interface SummaryPoller {
  start(materialId: string): void
  stop(): void
}

export function createSummaryPoller(options: SummaryPollerOptions): SummaryPoller {
  const intervalMs = options.intervalMs ?? 3000
  const timeoutMs = options.timeoutMs ?? 200_000
  let session = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function clearTimer(): void {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  function stop(): void {
    // 递增 session，使所有在途请求的旧 sessionId 立即失效
    session += 1
    clearTimer()
  }

  function start(materialId: string): void {
    stop()
    const mySession = ++session
    const deadline = Date.now() + timeoutMs
    const tick = async () => {
      if (mySession !== session) return
      if (Date.now() >= deadline) {
        options.onTimeout(materialId)
        return
      }
      let fetched: SummaryPollFetched | null = null
      try {
        fetched = await options.fetch(materialId)
      } catch {
        fetched = null // 单次失败不中断，下一轮重试
      }
      // 在途请求返回时 session 可能已切换（用户切走）→ 丢弃结果
      if (mySession !== session) return
      if (fetched && fetched.status !== 'pending') {
        options.onResult(materialId, {
          text: fetched.text,
          status: fetched.status,
          generatedAt: fetched.generatedAt,
        })
        return
      }
      timer = setTimeout(tick, intervalMs)
    }
    tick()
  }

  return { start, stop }
}
