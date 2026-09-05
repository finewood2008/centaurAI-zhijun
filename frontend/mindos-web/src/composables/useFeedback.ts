// 全局页面反馈：成功/错误/信息 Toast 与错误信息格式化。
import { useToast } from './useToast'

export function useFeedback() {
  const toast = useToast()
  return {
    success: (message: string) => toast({ type: 'success', message }),
    error: (message: string) => toast({ type: 'error', message }),
    info: (message: string) => toast({ type: 'info', message }),
  }
}

/** 从异常中提取可读信息，异常为空时使用兜底文案。 */
export function errorMessage(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}
