// text/event-stream 帧解析（纯函数，无 DOM 依赖，可在 node 下直接测试）。
//
// 规则（与 WHATWG SSE 一致的子集）：
// - 行以 \n 或 \r\n 结束；空行表示一帧结束；
// - `event: name` 设定事件名（默认 message）；`data: ...` 可多行，按 \n 拼接；
// - `id:` / `retry:` 忽略；以 `:` 开头的行是注释，忽略；
// - 不完整的尾部（未遇到空行）保留在缓冲区，等待下一块。
export interface SseFrame {
  event: string
  data: string
}

export function parseSseChunk(buffer: string, onFrame: (frame: SseFrame) => void): string {
  let rest = buffer
  for (;;) {
    // 一帧结束于一个空行：\n\n 或 \r\n\r\n（也容忍混合）
    const match = /\r?\n\r?\n/.exec(rest)
    if (!match) return rest
    const raw = rest.slice(0, match.index)
    rest = rest.slice(match.index + match[0].length)
    const frame = parseFrame(raw)
    if (frame) onFrame(frame)
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const data: string[] = []
  let sawField = false
  for (const line of raw.split(/\r?\n/)) {
    if (!line) continue
    if (line.startsWith(':')) continue // 注释/心跳
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') {
      event = value || 'message'
      sawField = true
    } else if (field === 'data') {
      data.push(value)
      sawField = true
    }
    // id / retry / 未知字段：忽略
  }
  if (!sawField) return null
  return { event, data: data.join('\n') }
}
