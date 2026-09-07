import catalog from '../../../shared/product-operations.json'

export interface ProductOperation {
  id: string
  method: string
  path: string
  pathParams: string[]
  query: string[]
  body: 'none' | 'json' | 'multipart'
  response: 'json' | 'sse' | 'bytes'
  maxRequestBytes: number
  maxResponseBytes: number
  capability: 'domain' | 'materials' | 'models'
  mutating: boolean
  idempotencyHeader?: 'Idempotency-Key'
  sensitiveResponse?: boolean
}
const operations = [...catalog.operations] as ProductOperation[]
operations.sort((a, b) => a.pathParams.length - b.pathParams.length || b.path.length - a.path.length)
const patterns = operations.map(operation => ({ operation,
  pattern: new RegExp('^' + operation.path.split(/(\{\w+\})/).map(part => /^\{/.test(part)
    ? '([^/]+)' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('') + '$'),
}))

/** Resolve an original product API call to a finite operation; never forward its URL. */
export function resolveProductOperation(path: string, method = 'GET'): {
  operation: ProductOperation; params: Record<string, string>; query: Record<string, string>
} {
  if (!path.startsWith('/api/') || /[#\\\r\n\0]/.test(path) || /%2f|%5c/i.test(path)) throw new Error('不支持的请求地址')
  const [pathname, search = ''] = path.split('?')
  if (path.split('?').length > 2) throw new Error('请求参数无效')
  for (const { operation, pattern } of patterns) {
    if (operation.method !== method) continue
    const match = pattern.exec(pathname)
    if (!match) continue
    const params: Record<string, string> = {}
    operation.pathParams.forEach((name, index) => {
      const value = decodeURIComponent(match[index + 1])
      if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value) || value === '.' || value === '..') throw new Error('对象编号无效')
      params[name] = value
    })
    const query: Record<string, string> = {}
    for (const [name, value] of new URLSearchParams(search)) {
      if (!operation.query.includes(name) || Object.prototype.hasOwnProperty.call(query, name) || value.length > 4000) throw new Error('请求参数无效')
      query[name] = value
    }
    return { operation, params, query }
  }
  throw new Error('当前连接不支持此产品操作')
}
