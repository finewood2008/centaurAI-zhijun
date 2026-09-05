// 裁决与导出的纯文案 / 命名逻辑（可在 node 下单测）。

export type ConflictKind = 'contradiction' | 'tension'

export const PURGE_PHRASE = '删除全部记忆'

export function conflictTitle(kind: ConflictKind | string): string {
  return kind === 'tension' ? '原则与做法有张力' : '两条理解看起来矛盾'
}

export function mergeLabel(p: { fromName?: string | null; intoName?: string | null; reason?: string }): string {
  const from = (p.fromName || '').trim() || '（未命名）'
  const into = (p.intoName || '').trim() || '（未命名）'
  const reason = (p.reason || '').trim()
  return reason ? `「${from}」和「${into}」可能是同一个（${reason}）` : `「${from}」和「${into}」可能是同一个`
}

export function exportFileName(date: Date = new Date()): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `zhijun-ontology-${y}${m}${d}.json`
}

export function purgeConfirmed(input: string | null | undefined): boolean {
  return (input || '').trim() === PURGE_PHRASE
}
