import { createConversation } from './api'
import { routingRequest } from './taskRouting'

export type MatterStatus = 'active' | 'paused' | 'completed'
export type ArtifactKind = 'communication' | 'decision_memo' | 'meeting_prep' | 'action_summary' | 'freeform'
export interface Matter {
  id: string; title: string; goal: string; context: string; nextStep: string; outcome: string
  status: MatterStatus; revision: number; decisionId: string | null; conversationId: string | null
  createdAt: string; updatedAt: string
}
export interface MatterArtifact {
  id: string; matterId: string; title: string; kind: ArtifactKind; markdown: string
  revision: number; userEdited: boolean; sourceMessageId: string; sourceConversationId: string
  createdAt: string; updatedAt: string
}
export interface MatterBinding { matter: Matter | null; bindingRevision: number }
export const artifactLabels: Record<ArtifactKind, string> = {
  communication: '重要沟通提纲', decision_memo: '决策备忘录', meeting_prep: '会前准备', action_summary: '行动小结', freeform: '自由文稿',
}
const path = (id: string) => '/mindos/matters/' + encodeURIComponent(id)
const bindingPath = (cid: string) => '/mindos/conversations/' + encodeURIComponent(cid) + '/matter'
export const listMatters = (status: MatterStatus | 'all' = 'active', signal?: AbortSignal) => routingRequest<{ items: Matter[]; total: number }>('/mindos/matters?status=' + status, 'GET', undefined, signal)
export const getMatter = (id: string) => routingRequest<Matter>(path(id))
export const getMatterBinding = (cid: string) => routingRequest<MatterBinding>(bindingPath(cid))
export const createMatter = (data: { requestId: string; title: string; conversationId?: string }) => routingRequest<Matter>('/mindos/matters', 'POST', data)
export const bindMatter = (cid: string, matterId: string | null, expectedRevision: number, requestId: string) => routingRequest<MatterBinding>(bindingPath(cid), 'PUT', { matterId, expectedRevision, requestId })
export const updateMatter = (id: string, data: Partial<Pick<Matter, 'title' | 'goal' | 'context' | 'nextStep' | 'outcome' | 'status'>> & { requestId: string; expectedRevision: number }) => routingRequest<Matter>(path(id), 'PATCH', data)
export const listArtifacts = (id: string) => routingRequest<{ items: MatterArtifact[]; total: number }>(path(id) + '/artifacts')
export const saveArtifact = (id: string, data: { requestId: string; conversationId: string; messageId: string; title?: string; kind: ArtifactKind }) => routingRequest<MatterArtifact>(path(id) + '/artifacts', 'POST', data)
export const updateArtifact = (id: string, data: { requestId: string; expectedRevision: number; title: string; markdown: string }) => routingRequest<MatterArtifact>('/mindos/artifacts/' + encodeURIComponent(id), 'PATCH', data)

/** Called only by an explicit user action. Opening an existing matter never makes a model request. */
const pendingConversations = new Map<string, string>()
export async function continueMatter(matter: Matter, requestId: string): Promise<string> {
  const fresh = await getMatter(matter.id)
  if (fresh.conversationId) { pendingConversations.delete(matter.id); return fresh.conversationId }
  let cid = pendingConversations.get(matter.id)
  if (!cid) { cid = (await createConversation({ title: fresh.title })).id; pendingConversations.set(matter.id, cid) }
  const binding = await getMatterBinding(cid)
  await bindMatter(cid, matter.id, binding.bindingRevision, requestId)
  pendingConversations.delete(matter.id)
  return cid
}
