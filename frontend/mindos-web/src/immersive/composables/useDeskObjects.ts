// 案头有没有东西：判断草稿（读当前轮桥接）+ 对话页投进 #zj-desk-host 的对象里「有内容」的那些。
// 「案」印只在 deskCount > 0 时才亮。宿主计数是模块级状态：Desk 抽屉挂观察器，印坞只读。
import { computed, readonly, ref, type ComputedRef, type Ref } from 'vue'
import { useActiveTurn } from '../activeTurn.ts'

/** 只用到 DOM 的这几个面，测试里可以用假对象。 */
export interface HostChildLike {
  classList: { contains(name: string): boolean }
  textContent: string | null
  childElementCount: number
  querySelector(selector: string): unknown
}

/**
 * 一个投进来的对象算不算「有内容」：
 * 事情与成果的触发钮只有绑定了事情才算；待核对只有条数大于 0 才算；本轮资料要有文件；章程只要出现就算；
 * 其余按有没有文字。
 */
export function hostChildHasContent(el: HostChildLike): boolean {
  const text = (el.textContent ?? '').trim()
  if (el.classList.contains('memory-pending-entry')) return el.querySelector('span') != null
  if (el.classList.contains('matter-trigger')) return text !== '' && text !== '事情与成果'
  if (el.classList.contains('chat-files')) return el.childElementCount > 0
  if (el.classList.contains('charter-chat')) return true
  return text !== ''
}

export function hostObjectCount(children: ArrayLike<HostChildLike>): number {
  let count = 0
  for (let i = 0; i < children.length; i += 1) if (hostChildHasContent(children[i]!)) count += 1
  return count
}

export function draftPresentOf(t: { draft: { status: string } | null; draftPending: boolean; draftTimedOut: boolean }): boolean {
  return (!!t.draft && t.draft.status !== 'discarded') || t.draftPending || t.draftTimedOut
}

export function deskCountOf(draftPresent: boolean, hostCount: number): number {
  return (draftPresent ? 1 : 0) + Math.max(0, hostCount)
}

const hostCount = ref(0)
const hostChildren = ref(0)

/** Desk 抽屉挂载宿主后调用；返回停止函数。 */
export function observeDeskHost(host: HTMLElement): () => void {
  const update = () => {
    hostChildren.value = host.childElementCount
    hostCount.value = hostObjectCount(host.children as unknown as ArrayLike<HostChildLike>)
  }
  update()
  const observer = new MutationObserver(update)
  observer.observe(host, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'hidden'] })
  return () => {
    observer.disconnect()
    hostCount.value = 0
    hostChildren.value = 0
  }
}

export interface DeskObjects {
  /** 亮「案」印的依据：草稿 + 宿主里有内容的对象。 */
  deskCount: ComputedRef<number>
  draftPresent: ComputedRef<boolean>
  /** 宿主里有内容的对象数。 */
  hostCount: Readonly<Ref<number>>
  /** 宿主里的元素数（含还没内容的触发钮），抽屉据此决定要不要显示这一节。 */
  hostChildren: Readonly<Ref<number>>
  /** 当前会话围绕一个判断：案头给「观察与复盘」入口。 */
  reviewAvailable: ComputedRef<boolean>
}

export function useDeskObjects(): DeskObjects {
  const turn = useActiveTurn()
  const draftPresent = computed(() => {
    const t = turn.value
    return !!t && draftPresentOf({ draft: t.draft.value, draftPending: t.draftPending.value, draftTimedOut: t.draftTimedOut.value })
  })
  const reviewAvailable = computed(() => !!turn.value?.decision.value)
  const deskCount = computed(() => deskCountOf(draftPresent.value, hostCount.value))
  return { deskCount, draftPresent, hostCount: readonly(hostCount), hostChildren: readonly(hostChildren), reviewAvailable }
}
