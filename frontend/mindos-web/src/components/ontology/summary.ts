import type { Claim, Section } from '@/services/api'

export type OntologyView = 'summary' | 'map' | 'list'
export function preferredOntologyView(saved: string | null): OntologyView {
  return saved === 'map' || saved === 'list' ? saved : 'summary'
}

export function summaryStatus(claim: Claim): string {
  if (claim.trustState === 'working') return '待你确认'
  if (claim.challenged) return '有不同说法，待核对'
  if (claim.layer === 'aspirational' || claim.selfAlignment?.framing === 'aspirational') return '理想方向，不等同于已实现'
  if (claim.scope === 'context_only' || claim.selfAlignment?.framing === 'context_only') return '只适用于当时情境'
  return '你已确认'
}

export function summaryDate(claim: Claim): string {
  return [claim.firstSeen, claim.lastReaffirmed].filter(Boolean).sort().pop() || ''
}

export function ontologySummary(claims: Claim[], now = Date.now()) {
  const visible = claims.filter(c => !c.retractedAt && !c.supersededById && ['confirmed', 'working'].includes(c.trustState))
  const dated = [...visible].sort((a, b) => (Date.parse(summaryDate(b)) || 0) - (Date.parse(summaryDate(a)) || 0))
  const confirmed = dated.filter(c => c.trustState === 'confirmed' && !c.challenged)
  const current = confirmed.filter(c => (!c.validFrom || Date.parse(c.validFrom) <= now) && (!c.validTo || Date.parse(c.validTo) > now))
  const stable = current.filter(c => c.scope !== 'context_only' && c.selfAlignment?.framing !== 'context_only' && c.layer !== 'aspirational' && c.selfAlignment?.framing !== 'aspirational')
  const group = (key: string, title: string, description: string, entries: Claim[], section: Section | 'inbox') => ({ key, title, description, items: entries.slice(0, 3), total: entries.length, section })
  return [
    group('roles', '身份与角色', '你已确认的身份、背景和承担的角色。', stable.filter(c => c.section === 'who'), 'who'),
    group('matters', '经历与正在做的事', '具体事情保留原有情境，不把经历等同于追求。', current.filter(c => c.section === 'matters'), 'matters'),
    group('principles', '在意的原则', '来自你确认过的理解；人生章程中的约定独立保留。', stable.filter(c => c.section === 'principles'), 'principles'),
    group('directions', '想走的方向', '愿望与现实分开看，也允许方向尚未清楚。', current.filter(c => c.section === 'direction' || c.layer === 'aspirational' || c.selfAlignment?.framing === 'aspirational'), 'direction'),
    group('uncertain', '仍待核对的理解', '这里尚无定论，由你决定如何修正或是否保留。', dated.filter(c => c.trustState === 'working' || c.challenged), 'inbox'),
    group('recent', '最近留下的记录', '展示新增或重新核对的原文，不自动推断你的变化。', confirmed, 'who'),
  ]
}
