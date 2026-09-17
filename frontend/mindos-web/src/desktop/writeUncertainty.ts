import type { DesktopSnapshot } from '../../../shared/desktop-contract'

export interface UncertainWriteOwner { accountId: string; deviceId: string; workspaceId: string }
export const uncertainWriteMessage = '连接中断时有操作尚未确认完成，结果可能已写入盒子。恢复后请先核对对话、资料或操作结果，不要直接重复提交。系统不会自动重发这些操作。'

/** Keep only a static warning for the same owner; never preserve old page data. */
export function nextWriteUncertainty(previous: DesktopSnapshot | null | undefined, next: DesktopSnapshot | null | undefined,
  notice: UncertainWriteOwner | null, hadPendingMutation: boolean): UncertainWriteOwner | null {
  const before = previous?.subject, after = next?.subject
  if (!after?.accountId) return null
  if (!after.deviceId) {
    // The normal reconnect CTA first disconnects and opens device selection.
    // Preserve only the generic warning until a different owner is selected.
    if (!['failed', 'disconnecting', 'selecting_device'].includes(next?.phase ?? '')) return null
    if (notice?.accountId === after.accountId) return notice
    return hadPendingMutation && previous?.phase === 'ready' && before?.workspaceId
      && before.accountId === after.accountId && before.deviceId
      ? { accountId: before.accountId, deviceId: before.deviceId, workspaceId: before.workspaceId } : null
  }
  let result = notice
  if (hadPendingMutation && previous?.phase === 'ready' && before?.workspaceId
      && before.accountId === after.accountId && before.deviceId === after.deviceId
      && (next?.generation !== previous.generation || next?.phase !== 'ready')) {
    result = { accountId: before.accountId, deviceId: before.deviceId!, workspaceId: before.workspaceId }
  }
  if (!result || result.accountId !== after.accountId || result.deviceId !== after.deviceId
      || (after.workspaceId && result.workspaceId !== after.workspaceId)) return null
  return result
}

export function showWriteUncertainty(notice: UncertainWriteOwner | null, snapshot: DesktopSnapshot | null | undefined): boolean {
  const subject = snapshot?.subject
  return !!notice && subject?.accountId === notice.accountId && (!subject.deviceId || subject.deviceId === notice.deviceId)
    && (!subject.workspaceId || subject.workspaceId === notice.workspaceId)
}
