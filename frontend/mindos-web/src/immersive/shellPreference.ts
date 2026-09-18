// 壳的偏好：`?shell=immersive|classic` → 写入 localStorage['zhijun.shell'] 并从地址栏清掉 → 否则读存储 → 默认经典壳。
// 第二阶段把默认改成沉浸壳；这里只负责读写，不碰路由表。
import { ref } from 'vue'

export const SHELL_STORAGE_KEY = 'zhijun.shell'
export type ShellChoice = 'immersive' | 'classic'

/** 从 search 或 hash（桌面端用 hash 路由）里取 shell 参数；不是这两个值就当没有。 */
export function shellParamFrom(search: string, hash = ''): ShellChoice | null {
  for (const part of [search, hash.includes('?') ? hash.slice(hash.indexOf('?')) : '']) {
    if (!part) continue
    const value = new URLSearchParams(part.startsWith('?') ? part.slice(1) : part).get('shell')
    if (value === 'immersive' || value === 'classic') return value
  }
  return null
}

/** 去掉 search / hash 里的 shell 参数，其余原样保留。 */
export function stripShellParam(search: string, hash = ''): { search: string; hash: string } {
  const clean = (part: string) => {
    if (!part) return ''
    const params = new URLSearchParams(part.startsWith('?') ? part.slice(1) : part)
    params.delete('shell')
    const rest = params.toString()
    return rest ? `?${rest}` : ''
  }
  const hashIndex = hash.indexOf('?')
  const nextHash = hashIndex >= 0 ? hash.slice(0, hashIndex) + clean(hash.slice(hashIndex)) : hash
  return { search: clean(search), hash: nextHash }
}

/** 纯函数：参数优先，其次是已存的选择，默认 false。 */
export function resolveShellPreference(param: ShellChoice | null, stored: string | null): boolean {
  if (param) return param === 'immersive'
  return stored === 'immersive'
}

function readStored(): string | null {
  try { return localStorage.getItem(SHELL_STORAGE_KEY) } catch { return null }
}

function writeStored(choice: ShellChoice): void {
  try { localStorage.setItem(SHELL_STORAGE_KEY, choice) } catch { /* 无法持久化时只在本次会话生效 */ }
}

function readInitial(): boolean {
  if (typeof window === 'undefined') return false
  try {
    const param = shellParamFrom(window.location.search, window.location.hash)
    if (param) {
      writeStored(param)
      const { search, hash } = stripShellParam(window.location.search, window.location.hash)
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${search}${hash}`)
    }
    return resolveShellPreference(param, readStored())
  } catch {
    return false
  }
}

export const immersiveShell = ref(readInitial())

export function setImmersiveShell(on: boolean): void {
  immersiveShell.value = on
  writeStored(on ? 'immersive' : 'classic')
}
