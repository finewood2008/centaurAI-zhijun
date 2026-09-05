// 语音输入的纯逻辑：只在浏览器有 SpeechRecognition 时启用；永远不自动发送。
export function speechSupported(): boolean {
  if (typeof window === 'undefined') return false
  const w = window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown }
  return Boolean(w.SpeechRecognition || w.webkitSpeechRecognition)
}

export function createRecognizer(): any | null {
  if (!speechSupported()) return null
  const w = window as unknown as { SpeechRecognition?: any; webkitSpeechRecognition?: any }
  const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition
  const rec = new Ctor()
  rec.lang = 'zh-CN'
  rec.continuous = true
  rec.interimResults = true
  return rec
}

/**
 * 把「开始录音前的文本 base」「已定稿的识别结果 finalText」「仍在修正的临时结果 interimText」合成输入框内容。
 * 规则：base 保留在最前；final 追加其后；interim 永远放在最后并随识别刷新被替换。
 */
export function mergeTranscript(base: string, finalText: string, interimText: string): string {
  const head = (base || '').replace(/\s+$/, '')
  const fin = (finalText || '').trim()
  const tmp = (interimText || '').trim()
  const parts = [head, fin, tmp].filter((p) => p.length > 0)
  return parts.join(head && (fin || tmp) ? '\n' : '')
}

/** 从 SpeechRecognitionEvent 的 results 里分离已定稿与临时文本（纯函数，便于测试）。 */
export function splitResults(results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>): { finalText: string; interimText: string } {
  let finalText = ''
  let interimText = ''
  for (let i = 0; i < results.length; i += 1) {
    const r = results[i]
    const t = r[0]?.transcript ?? ''
    if (r.isFinal) finalText += t
    else interimText += t
  }
  return { finalText, interimText }
}
