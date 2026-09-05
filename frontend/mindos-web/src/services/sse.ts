// SSE 流式客户端：POST + fetch + ReadableStream。
//
// 不能用 EventSource——它只支持 GET 且无法带 X-Requested-By / X-MindOS-Session，
// 会被后端的 loopback + CSRF + 票据会话三层 gate 拒绝。这里复用 api.ts 的
// buildHeaders / throwApiError，保证与普通请求完全一致的鉴权行为。
import { API_BASE, ApiError, buildHeaders, throwApiError } from './api'
import { parseSseChunk, type SseFrame } from '@/shared/sse-parser'

export { parseSseChunk }
export type { SseFrame }

export type SseHandlers = Record<string, (data: unknown) => void>

/**
 * 发起流式 POST。流开始前的非 2xx 抛 ApiError；流中的每一帧按 event 名分发到
 * handlers（data 解析为 JSON，解析失败时原样传字符串）。signal 用于中断。
 */
export async function streamPost(
  path: string,
  body: unknown,
  handlers: SseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers = buildHeaders()
  headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'text/event-stream')
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body ?? {}),
    signal,
  })
  if (!res.ok) await throwApiError(res)
  if (!res.body) throw new ApiError('服务端未返回流式响应', res.status)

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  const dispatch = (frame: SseFrame) => {
    const handler = handlers[frame.event]
    if (!handler) return
    let payload: unknown = frame.data
    try {
      payload = JSON.parse(frame.data)
    } catch {
      // 非 JSON 数据原样透传
    }
    handler(payload)
  }
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      buffer = parseSseChunk(buffer, dispatch)
    }
    buffer += decoder.decode()
    // 流结束时若尾部还有未以空行收尾的完整帧，补一个分隔符再解析一次
    if (buffer.trim()) parseSseChunk(`${buffer}\n\n`, dispatch)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // 已释放
    }
  }
}
