// 应用级响应式 Toast store：
// ToastHost 渲染此列表；任何模块（含 main.ts 全局错误处理）都可直接 pushToast，无需组件上下文。
import { reactive } from 'vue'

export type ToastType = 'success' | 'error' | 'info'

export interface ToastOptions {
  type?: ToastType
  message: string
}

export interface ToastItem {
  id: number
  type: ToastType
  message: string
  closing: boolean
}

const toasts = reactive<ToastItem[]>([])
let seq = 0

const AUTO_CLOSE_MS = 2500
const EXIT_MS = 200

export function closeToast(id: number) {
  const item = toasts.find((t) => t.id === id)
  if (!item || item.closing) return
  item.closing = true
  window.setTimeout(() => {
    const idx = toasts.findIndex((t) => t.id === id)
    if (idx >= 0) toasts.splice(idx, 1)
  }, EXIT_MS)
}

export function pushToast(options: ToastOptions): void {
  const id = ++seq
  toasts.push({ id, type: options.type ?? 'info', message: options.message, closing: false })
  window.setTimeout(() => closeToast(id), AUTO_CLOSE_MS)
}

export function useToastList(): ToastItem[] {
  return toasts
}
