// 昼夜色调：按小时在壳根元素上写 --zj-tint。早晨偏暖、白天中性、夜里偏灰蓝；每 10 分钟重算。
// 只是一层极淡的罩，不做深色模式的机械反转。
import { getCurrentInstance, onBeforeUnmount, onMounted, type Ref } from 'vue'

export const TINT_MORNING = 'rgba(166, 69, 46, 0.035)'
export const TINT_DAY = 'transparent'
export const TINT_NIGHT = 'rgba(60, 64, 61, 0.04)'
export const TINT_INTERVAL_MS = 10 * 60 * 1000

/** 纯函数：5 到 10 点偏暖，10 到 18 点中性，其余偏冷。 */
export function tintForHour(hour: number): string {
  const h = ((Math.floor(hour) % 24) + 24) % 24
  if (h >= 5 && h < 10) return TINT_MORNING
  if (h >= 10 && h < 18) return TINT_DAY
  return TINT_NIGHT
}

export function useDayTint(root: Ref<HTMLElement | null>, now: () => Date = () => new Date()): { apply: () => void } {
  let timer: ReturnType<typeof setInterval> | undefined
  const apply = () => { root.value?.style.setProperty('--zj-tint', tintForHour(now().getHours())) }
  if (getCurrentInstance()) {
    onMounted(() => { apply(); timer = setInterval(apply, TINT_INTERVAL_MS) })
    onBeforeUnmount(() => clearInterval(timer))
  }
  return { apply }
}
