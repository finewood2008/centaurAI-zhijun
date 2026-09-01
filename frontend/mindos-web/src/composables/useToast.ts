// Toast 反馈入口：返回 store 的 pushToast，任何组件内直接可用。
import { pushToast, type ToastOptions } from './toastStore'

export type ToastFn = (options: ToastOptions) => void

export function useToast(): ToastFn {
  return pushToast
}
