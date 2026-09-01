// P14-09：关联加载防串台辅助（知识卡片编辑页使用）。
//
// 与原材料详情页的会话闸门（MaterialDetailPage 的 relatedLoadGate）语义一致：
// - load(id) 开始时开启新请求代次，并立即清空旧结果（切换卡片后详情已显示、关联
//   仍在加载时，不短暂显示旧卡片的关联推荐）；
// - 请求返回后，只有“仍是当前代次”且“当前路由目标仍为 id”时才写回——旧卡片的
//   延迟关联请求不会覆盖新卡片结果；
// - 组件卸载时 invalidate() 使在途请求全部失效。
//
// 内联会话门逻辑（与 composables/sessionGate.ts 语义一致）而非 import 它：
// 避免 Node --experimental-strip-types 直跑测试时对无扩展名相对导入的解析问题。
interface LoaderGate {
  next(): number
  isCurrent(id: number): boolean
  invalidate(): void
}

function createLoaderGate(): LoaderGate {
  let session = 0
  return {
    next() {
      return ++session
    },
    isCurrent(id) {
      return id === session
    },
    invalidate() {
      session += 1
    },
  }
}

export interface RelatedLoaderOptions<T> {
  /** 发起关联请求。 */
  fetch: (id: string) => Promise<T>
  /** 当前路由目标是否仍为 id（防串页二次校验）。 */
  isCurrentTarget: (id: string) => boolean
  /** 请求成功且校验通过时写回结果。 */
  onResult: (data: T) => void
  /** 新请求开启 / 校验失败 / 异常时清空旧结果。 */
  onEmpty: () => void
  /** 加载中状态。 */
  onLoading: (loading: boolean) => void
}

export interface RelatedLoader {
  load: (id: string) => Promise<void>
  invalidate: () => void
}

export function createRelatedLoader<T>(options: RelatedLoaderOptions<T>): RelatedLoader {
  const gate: LoaderGate = createLoaderGate()
  return {
    async load(id: string) {
      options.onLoading(true)
      const session = gate.next()
      // 新请求开启即作废旧结果，避免短暂展示上一张卡片的关联推荐
      options.onEmpty()
      try {
        const data = await options.fetch(id)
        if (!gate.isCurrent(session) || !options.isCurrentTarget(id)) return
        options.onResult(data)
      } catch {
        // 异常仅在当前代次且目标一致时清空，避免覆盖已就绪的新卡片空态
        if (gate.isCurrent(session) && options.isCurrentTarget(id)) options.onEmpty()
      } finally {
        if (gate.isCurrent(session) && options.isCurrentTarget(id)) options.onLoading(false)
      }
    },
    invalidate() {
      gate.invalidate()
    },
  }
}
