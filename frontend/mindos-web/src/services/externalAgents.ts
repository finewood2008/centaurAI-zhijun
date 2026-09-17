export const PERSONAL_SECTIONS = [
  ['who', '我是谁'], ['people', '我的人'], ['matters', '我的事'],
  ['principles', '我的原则'], ['ways', '我的做法'], ['direction', '我的方向'],
] as const
export type PersonalSection = typeof PERSONAL_SECTIONS[number][0]
export interface GrantFields {
  agentId: string; agentName: string; sections: PersonalSection[]; materialIds: string[]
  excludedClaimIds: string[]; acknowledgedLegacyIds: string[]; days: 1 | 7 | 30; disclosureAccepted: true
}
export interface AgentGrant extends GrantFields {
  id: string; revision: number; state: 'active' | 'paused' | 'revoked'; expiresAt: number; createdAt: number
}
export interface GrantUpdate extends GrantFields { expectedRevision: number }
export interface ExternalAgentStatus { available: boolean; enabled: boolean; endpoint: string | null; grants: AgentGrant[] }
/** Status reads can fail as HTTP ApiError or desktop ProductFailure. */
export function externalAgentsUnavailable(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const { status, code, remoteCode } = error as { status?: number; code?: string; remoteCode?: string }
  if (status === 401 || status === 403) {
    return status === 403 && (code === 'WORKSPACE_OPERATION_DENIED'
      || (code === 'ACCESS_DENIED' && remoteCode === 'WORKSPACE_OPERATION_DENIED'))
  }
  // A desktop REMOTE_ERROR 404 can mean a missing job, not a missing route.
  return ((status === 404 || status === 501) && code !== 'REMOTE_ERROR')
    || ((status === 400 || status === 501 || status === 503)
      && (code === 'WORKER_OPERATION_INVALID' || code === 'PRODUCT_OPERATION_UNSUPPORTED'))
}
export interface AccessPreview {
  personal: { id: string; content: string; section: PersonalSection; nature: string; requiresLegacyConfirmation: boolean }[]
  materials: { id: string; title: string; version: number; updatedAt: string | null }[]
}
export interface AccessAudit {
  id: string; agent: string; grant_id: string; operation: string; result: string; delivery: string; created: number
  resources: { id: string; version: string | number }[]
}
export function grantLabel(grant: AgentGrant, now = Date.now()): string {
  if (grant.state === 'revoked') return '已撤销'
  if (grant.expiresAt * 1000 <= now) return '已到期'
  return grant.state === 'paused' ? '已暂停' : '授权中'
}
export function editableGrant(grant: AgentGrant): GrantUpdate {
  return { agentId: grant.agentId, agentName: grant.agentName, sections: [...grant.sections], materialIds: [...grant.materialIds],
    excludedClaimIds: [...grant.excludedClaimIds], acknowledgedLegacyIds: [...grant.acknowledgedLegacyIds],
    days: grant.days, disclosureAccepted: true, expectedRevision: grant.revision }
}
