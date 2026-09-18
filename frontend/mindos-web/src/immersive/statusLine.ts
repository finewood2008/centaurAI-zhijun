// 知君气泡下那一行极淡的状态文字。模板生成，不调模型，不出现服务商与模型名。
// 有 contextPlan 时按明确引用计数（citedRefs）；旧回执按关联记录计数；「· 在线 / 本机」后缀默认关。
import type { MessageStatus, ProvenanceEvent } from '@/services/api'
import { contextItems, normalizeProvenance } from '../shared/provenanceGraph.ts'

export type StatusLineStatus = MessageStatus | 'streaming' | null | undefined

export interface StatusLineOptions {
  /** 追加「· 在线」/「· 本机」；默认不显示。 */
  channel?: boolean
}

export function statusLineText(
  provenance: Partial<ProvenanceEvent> | null | undefined,
  meta: { external?: boolean } | null | undefined,
  status: StatusLineStatus,
  initiated = false,
  options: StatusLineOptions = {},
): string {
  if (status === 'streaming') return '在想'
  if (status === 'aborted') return '你按了停止，这段没有说完'
  if (status === 'error') return '这一轮没有完成'
  const parts: string[] = []
  if (provenance) {
    const p = normalizeProvenance(provenance)
    if (p.contextPlan) {
      const cited = contextItems(p.contextPlan, 'citedRefs').length
      const provided = contextItems(p.contextPlan, 'providedRefs').length
      if (cited > 0) parts.push(`参考了你记下的 ${cited} 条`)
      else if (provided > 0) parts.push(`看了你记下的 ${provided} 条，这次没有直接引用`)
      else if (!initiated) parts.push('没有参考记下的内容')
      if (p.contextPlan.stage === 'supplemented') parts.push('补查了一次')
      else if (p.contextPlan.stage === 'lookup_unavailable') parts.push('补查没有完成')
    } else {
      const recorded = p.confirmedClaims.length + p.workingClaims.length + p.materials.length
      if (recorded > 0) parts.push(`参考了你记下的 ${recorded} 条`)
      else if (!initiated) parts.push('没有参考记下的内容')
    }
    if (p.retractedNotices > 0) parts.push(`避开了 ${p.retractedNotices} 条你纠正过的`)
  } else if (!initiated) {
    parts.push('没有参考记下的内容')
  }
  if (options.channel && meta && typeof meta.external === 'boolean') parts.push(meta.external ? '在线' : '本机')
  return parts.join(' · ')
}
