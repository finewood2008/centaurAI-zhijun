// 壳的偏好：`?shell=immersive|classic` → 写入 localStorage['zhijun.shell'] 并从地址栏清掉 → 否则读存储 → 默认沉浸壳。
// 2026-09-21（PRD V2 的 P0）：默认从经典壳改为沉浸壳。经典壳保留，偏好页可随时切回，
// 只有存储里明确写着 'classic' 才走经典壳——读不到存储、存储损坏、全新设备都走沉浸壳。
// 这里只负责读写，不碰路由表。
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

/** 纯函数：参数优先，其次是已存的选择，默认沉浸壳。只有明确存着 'classic' 才是经典壳。 */
export function resolveShellPreference(param: ShellChoice | null, stored: string | null): boolean {
  if (param) return param === 'immersive'
  return stored !== 'classic'
}

function readStored(): string | null {
  try { return localStorage.getItem(SHELL_STORAGE_KEY) } catch { return null }
}

function writeStored(choice: ShellChoice): void {
  try { localStorage.setItem(SHELL_STORAGE_KEY, choice) } catch { /* 无法持久化时只在本次会话生效 */ }
}

function readInitial(): boolean {
  if (typeof window === 'undefined') return true
  try {
    const param = shellParamFrom(window.location.search, window.location.hash)
    if (param) {
      writeStored(param)
      const { search, hash } = stripShellParam(window.location.search, window.location.hash)
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${search}${hash}`)
    }
    return resolveShellPreference(param, readStored())
  } catch {
    return true
  }
}

export const immersiveShell = ref(readInitial())

export function setImmersiveShell(on: boolean): void {
  immersiveShell.value = on
  writeStored(on ? 'immersive' : 'classic')
}
