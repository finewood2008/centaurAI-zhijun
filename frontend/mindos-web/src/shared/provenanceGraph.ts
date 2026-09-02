// 回复出处小图的纯数据整形：把一轮的 provenance 拆成最多四组来源，供 SVG 渲染与测试。
import type { ProvenanceEvent } from '@/services/api'

export const MAX_SHOWN = 8

export type ProvGroupKey = 'confirmed' | 'working' | 'materials' | 'retracted'

export interface ProvItem {
  id: string
  label: string
  kind: 'claim' | 'working' | 'material' | 'retracted'
  section?: string
  layer?: string
  materialId?: string
}

export interface ProvGroup {
  key: ProvGroupKey
  label: string
  note?: string
  items: ProvItem[]
  shown: ProvItem[]
  extra: number
  count: number
}

export function lineWidth(count: number): number {
  if (count <= 0) return 1
  return Math.max(1, Math.min(3, Math.ceil(count / 2)))
}

export function truncateTitle(text: string | null | undefined, max = 10): string {
  const t = (text ?? '').trim()
  if (!t) return ''
  return t.length > max ? `${t.slice(0, max)}…` : t
}

function makeGroup(key: ProvGroupKey, label: string, items: ProvItem[], count: number, note?: string): ProvGroup {
  return { key, label, note, items, shown: items.slice(0, MAX_SHOWN), extra: Math.max(0, items.length - MAX_SHOWN), count }
}

/** 只返回有内容的组（顺序：已确认 → 工作理解 → 资料 → 避开的旧理解）。 */
export function groups(p: ProvenanceEvent | null | undefined): ProvGroup[] {
  if (!p) return []
  const out: ProvGroup[] = []
  const confirmed = (p.confirmedClaims ?? []).map<ProvItem>((c) => ({ id: c.id, label: c.content, kind: 'claim', section: c.section, layer: c.layer }))
  if (confirmed.length) out.push(makeGroup('confirmed', '已确认的理解', confirmed, confirmed.length))
  const working = (p.workingClaims ?? []).map<ProvItem>((c) => ({ id: c.id, label: c.content, kind: 'working', section: c.section, layer: c.layer }))
  if (working.length) out.push(makeGroup('working', '工作理解', working, working.length, '带保留语气'))
  const materials = (p.materials ?? []).map<ProvItem>((m, i) => ({ id: `${m.materialId}-${i}`, label: m.title || m.materialId, kind: 'material', materialId: m.materialId }))
  if (materials.length) out.push(makeGroup('materials', '资料片段', materials, materials.length))
  const retracted = Number(p.retractedNotices) || 0
  if (retracted > 0) out.push(makeGroup('retracted', '避开的旧理解', [{ id: 'retracted', label: `避开 ${retracted} 条被你纠正的`, kind: 'retracted' }], retracted))
  return out
}

export function isEmpty(p: ProvenanceEvent | null | undefined): boolean {
  return groups(p).length === 0
}
