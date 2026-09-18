// 回复节奏的偏好：localStorage['zhijun.pacedReply']，默认开；「off」关闭。偏好卡里的开关写这里。
import { ref } from 'vue'

export const PACED_REPLY_STORAGE_KEY = 'zhijun.pacedReply'

export function resolvePacedReplyPreference(stored: string | null | undefined): boolean {
  return stored !== 'off'
}

function readStored(): string | null {
  try { return localStorage.getItem(PACED_REPLY_STORAGE_KEY) } catch { return null }
}

export const pacedReplyEnabled = ref(typeof window === 'undefined' ? true : resolvePacedReplyPreference(readStored()))

export function setPacedReply(on: boolean): void {
  pacedReplyEnabled.value = on
  try { localStorage.setItem(PACED_REPLY_STORAGE_KEY, on ? 'on' : 'off') } catch { /* 无法持久化时只在本次会话生效 */ }
}
