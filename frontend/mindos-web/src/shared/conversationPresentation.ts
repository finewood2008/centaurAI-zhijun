export const CHAT_UNAVAILABLE = '暂时无法回答，请重试'

// Keep actionable permission and data errors; provider diagnostics stay in logs.
export function conversationNotice(message: string | null | undefined, fallback = CHAT_UNAVAILABLE): string {
  if (!message) return fallback
  return /模型|供应商|provider|ollama|deepseek|openai|qwen|claude|api.?key|https?:\/\//i.test(message) ? fallback : message
}
