// 分析轮询器（P14-04 / P0-1）。
//
// 复用 P14-03 摘要轮询器的 session token 防串台机制：start/stop 都递增 session，
// 每个 tick 捕获自己的 sessionId，写回结果前必须校验 sessionId 仍是当前 session，
// 轮询 GET /materials/{id}/analysis，直到「摘要、标签候选、实体、关系三元组」都离开
// pending 终态；这样「资料 A 的在途分析请求延迟返回、用户已切到资料 B」时，
// A 的结果不会覆盖 B 的候选 / 实体 / 关系 / 摘要。
import type { MaterialAnalysis } from '../services/api'

// 单次轮询的原始响应，与 GET /analysis 返回的 MaterialAnalysis 结构对齐
export type AnalysisPollFetched = MaterialAnalysis

export interface AnalysisPollResult {
  summary: AnalysisPollFetched['summary']
  tagSuggestions: AnalysisPollFetched['tagSuggestions']
  entities: AnalysisPollFetched['entities']
  relations: AnalysisPollFetched['relations']
}

export interface AnalysisPollerOptions {
  // 单次轮询请求；失败时 reject（调用方可选择在下一轮重试）
  fetch: (materialId: string) => Promise<AnalysisPollFetched>
  // 轮询到摘要、标签候选、实体与关系均非 pending 终态时回调（调用方负责校验当前资料仍一致）
  onResult: (materialId: string, result: AnalysisPollResult) => void
  // 等待预算耗尽仍未完成时回调
  onTimeout: (materialId: string) => void
  intervalMs?: number
  timeoutMs?: number
}

export interface AnalysisPoller {
  start(materialId: string): void
  stop(): void
}

function isDone(result: AnalysisPollFetched): boolean {
  return (
    result.summary.status !== 'pending' &&
    result.tagSuggestions.status !== 'pending' &&
    result.entities.status !== 'pending' &&
    result.relations.status !== 'pending'
  )
}

export function createAnalysisPoller(options: AnalysisPollerOptions): AnalysisPoller {
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
      let fetched: AnalysisPollFetched | null = null
      try {
        fetched = await options.fetch(materialId)
      } catch {
        fetched = null // 单次失败不中断，下一轮重试
      }
      // 在途请求返回时 session 可能已切换（用户切走）→ 丢弃结果
      if (mySession !== session) return
      if (fetched && isDone(fetched)) {
        options.onResult(materialId, {
          summary: fetched.summary,
          tagSuggestions: fetched.tagSuggestions,
          entities: fetched.entities,
          relations: fetched.relations,
        })
        return
      }
      timer = setTimeout(tick, intervalMs)
    }
    tick()
  }

  return { start, stop }
}
