// 会话门（资料切换期间异步防串台）。
//
// 为「详情加载」「摘要重试」等异步请求提供不可复用的请求代次：
// - next() 开启一次新请求，使所有旧代次立即失效；
// - 请求返回后，只有 isCurrent(requestSession) 为真（且路由/当前资料仍一致）才能
//   写入页面状态；
// - 组件卸载时 invalidate() 使在途请求全部失效。
export interface SessionGate {
  /** 开始新一次请求，返回本次请求代次；同时使旧代次失效。 */
  next(): number
  /** 判断某代次是否仍是最新。 */
  isCurrent(id: number): boolean
  /** 使当前所有在途代次失效（组件卸载时调用）。 */
  invalidate(): void
}

export function createSessionGate(): SessionGate {
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
