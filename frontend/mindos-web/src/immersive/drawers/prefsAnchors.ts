// 偏好抽屉顶部的锚点：跳到 WorkspaceSettings 既有的 h2 分区、账号与盒子（桌面端）、外部 Agent 与「高级」。
// 用标题文字或选择器在抽屉正文里找目标；找不到的锚点不显示。
export interface PrefsAnchor {
  id: string
  label: string
  /** 按 h2 开头文字找（WorkspaceSettings 的分区没有 id）。 */
  heading?: string
  /** 按选择器找。 */
  selector?: string
}

export const PREFS_ADVANCED_ID = 'zj-prefs-advanced'

export const PREFS_ANCHORS: ReadonlyArray<PrefsAnchor> = [
  { id: 'relationship', label: '关系设置', heading: '关系设置' },
  { id: 'memory', label: '记忆整理', heading: '记忆整理' },
  { id: 'chat', label: '日常对话与理解', heading: '日常对话与理解' },
  { id: 'files', label: '本地文件处理', heading: '本地文件处理' },
  { id: 'monitor', label: '运行监控与任务', heading: '运行监控与任务' },
  { id: 'box', label: '账号与盒子', selector: '[data-testid="box-settings"]' },
  { id: 'agents', label: '外部 Agent', selector: '#external-agents-heading' },
  { id: 'advanced', label: '高级', selector: `#${PREFS_ADVANCED_ID}` },
]

export interface AnchorRootLike {
  querySelector(selector: string): Element | null
  querySelectorAll(selector: string): ArrayLike<{ textContent: string | null }> & Iterable<{ textContent: string | null }>
}

export function findAnchorTarget(root: AnchorRootLike, anchor: PrefsAnchor): Element | null {
  if (anchor.selector) return root.querySelector(anchor.selector)
  if (!anchor.heading) return null
  for (const heading of root.querySelectorAll('h2')) {
    if ((heading.textContent ?? '').trim().startsWith(anchor.heading)) return heading as Element
  }
  return null
}

export function availableAnchorIds(root: AnchorRootLike): Set<string> {
  const ids = new Set<string>()
  for (const anchor of PREFS_ANCHORS) if (findAnchorTarget(root, anchor)) ids.add(anchor.id)
  return ids
}
