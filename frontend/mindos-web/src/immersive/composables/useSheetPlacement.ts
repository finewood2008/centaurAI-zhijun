// 抽屉的落点：手机宽度（≤767px）从底部升起（sheet），其余从右侧滑出（side）。matchMedia 变化时实时切换。
import { getCurrentInstance, onBeforeUnmount, ref, type Ref } from 'vue'

export const PHONE_QUERY = '(max-width: 767px)'
export type DrawerPlacement = 'side' | 'sheet'

/** 纯函数：是否手机宽度 → 落点。 */
export function placementFor(phone: boolean): DrawerPlacement {
  return phone ? 'sheet' : 'side'
}

export function useSheetPlacement(): Readonly<Ref<DrawerPlacement>> {
  const placement = ref<DrawerPlacement>('side')
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return placement
  const query = window.matchMedia(PHONE_QUERY)
  const update = () => { placement.value = placementFor(query.matches) }
  update()
  query.addEventListener('change', update)
  if (getCurrentInstance()) onBeforeUnmount(() => query.removeEventListener('change', update))
  return placement
}
