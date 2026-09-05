// 统一的来源 / 纠错跳转路径构造（P14-12）。
// 自包含无依赖，可被 node --experimental-strip-types 直接测试。
export function sourceRoute(sourceId: string): string {
  const encoded = encodeURIComponent(sourceId)
  return sourceId.startsWith('knowledge_') ? `/knowledge/${encoded}` : `/materials/${encoded}`
}

export function correctionDetailRoute(correctionId: string): string {
  return `/corrections?correctionId=${encodeURIComponent(correctionId)}`
}
