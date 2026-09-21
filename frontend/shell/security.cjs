'use strict'
const { realpath, readFile } = require('node:fs/promises')
const path = require('node:path')
const ENTRY_URL = 'zhijun://desktop/desktop.html'
const INVOKE_CHANNEL = 'zhijun:invoke'
const SNAPSHOT_CHANNEL = 'zhijun:snapshot'
const OPERATIONS = new Set(['getSnapshot', 'getRememberedLogin', 'beginSignIn', 'signInWithPassword', 'signInWithSavedPassword',
  'sendRegistrationCode', 'resetPassword', 'registerWithPassword', 'listDevices', 'claimDevice', 'connect',
  'openProvisioning', 'disconnect', 'signOut', 'materials.list', 'cancelRead',
  ...require('./runtime/product-session.cjs').productMethods.map(method => `product.${method}`)])
const CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: zhijun-media:; media-src blob: zhijun-media:; font-src 'self'; connect-src zhijun-media: blob:; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'"

function shouldBlockRendererRequest(value) {
  try {
    const url = new URL(value)
    if (url.protocol === 'zhijun:') return !(url.host === 'desktop' && !url.username && !url.password)
    if (url.protocol === 'zhijun-media:') return !(url.host === 'session' && !url.username && !url.password
      && !url.search && !url.hash && /^\/[a-f0-9]{32}$/.test(url.pathname))
    if (url.protocol === 'blob:') return !value.startsWith('blob:zhijun://desktop/')
    if (url.protocol === 'data:') return !/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value)
    return true
  } catch { return true }
}

function isEntryUrl(value) {
  try { const url = new URL(value); url.hash = ''; return url.href === ENTRY_URL } catch { return false }
}
function isTrustedSender(event, contents) {
  return Boolean(contents && !contents.isDestroyed() && event?.sender === contents &&
    event.senderFrame === contents.mainFrame && isEntryUrl(event.senderFrame?.url))
}
function denied(runtime, code = 'ACCESS_DENIED') {
  return { ok: false, generation: runtime.snapshot().generation,
    error: { code, message: code === 'ACCESS_DENIED' ? '此页面无权调用桌面功能。' : '桌面操作参数无效。', recovery: 'none' } }
}
function createInvokeHandler(runtime, getContents) {
  return async (event, operation, args) => {
    const contents = getContents()
    if (!isTrustedSender(event, contents)) return denied(runtime)
    if (typeof operation !== 'string' || !OPERATIONS.has(operation) || !Array.isArray(args) || args.length > 2) {
      return denied(runtime, 'INVALID_REQUEST')
    }
    if (['product.requestMicrophone', 'openProvisioning'].includes(operation) && !contents.isFocused()) return denied(runtime)
    return runtime.invoke(operation, args, contents.id)
  }
}
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2' }
// Only bundled assets are readable; this is not a general file proxy.
function createAssetHandler(assetRoot) {
  return async request => {
    const headers = { 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store', 'Permissions-Policy': 'microphone=(self), camera=(), display-capture=()' }
    try {
      const url = new URL(request.url)
      if (!['GET', 'HEAD'].includes(request.method) || url.protocol !== 'zhijun:' ||
          url.host !== 'desktop' || url.username || url.password || url.search) {
        return new Response(null, { status: 403, headers })
      }
      const pathname = decodeURIComponent(url.pathname)
      if (pathname !== '/desktop.html' && !/^\/assets\/[A-Za-z0-9._-]+$/.test(pathname)) {
        return new Response(null, { status: 404, headers })
      }
      const mime = MIME[path.extname(pathname)]
      if (!mime || (pathname !== '/desktop.html' && mime.startsWith('text/html'))) {
        return new Response(null, { status: 404, headers })
      }
      const root = await realpath(assetRoot)
      const target = await realpath(path.join(root, pathname.slice(1)))
      if (!target.startsWith(root + path.sep)) return new Response(null, { status: 403, headers })
      headers['Content-Type'] = mime
      return new Response(request.method === 'HEAD' ? null : await readFile(target), { status: 200, headers })
    } catch { return new Response(null, { status: 404, headers }) }
  }
}
// ---------------------------------------------------------------------------
// 本机模式（第二形态，见 docs/development/local-mode.md）
//
// 盒端形态靠「渲染进程绝不直接联网」这条保证：入口是自定义协议、请求拦截器只放行
// zhijun: / zhijun-media: / blob: / data:image、CSP 的 connect-src 不含 http。
// 本机模式恰恰相反——页面由本机后端自己服务，必须能同源 fetch 它。
//
// 这里**不改动上面那套**，只另给一份本机模式的判定。两份各自独立，盒端那套的
// 保证一个字没动。
//
// 一个关键的设计选择：**本机模式不暴露任何 IPC。** 它加载的是网页构建
// （src/main.ts），那份代码根本不调 window.zhijunDesktop。不暴露就等于绕开了
// 「isEntryUrl 同时把守 isTrustedSender」这个最危险的耦合——放宽入口判定去容纳
// 本机地址，本来会连带放宽谁能调用桌面能力。现在这个问题不存在。
// ---------------------------------------------------------------------------

/** 本机后端只认字面回环地址。不接受 localhost：那是个名字，可以被解析到别处。 */
function createLocalProfile(port) {
  const parsed = Number(port)
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error('本机模式需要一个合法端口')
  }
  const origin = `http://127.0.0.1:${parsed}`
  const entryUrl = `${origin}/mindos/`
  // 'self' 在这里解析成上面那个 origin，正好是本机后端，不会多放行一个来源。
  const csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    + "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; "
    + "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'"

  const sameOrigin = value => {
    try {
      const url = new URL(value)
      return url.protocol === 'http:' && url.hostname === '127.0.0.1'
        && url.port === String(parsed) && !url.username && !url.password
    } catch { return false }
  }

  return {
    mode: 'local',
    origin,
    entryUrl,
    CSP: csp,
    /** 入口与应用内的整页导航都在 /mindos/ 之下；别的路径一律不是入口。 */
    isEntryUrl(value) {
      if (!sameOrigin(value)) return false
      try { return new URL(value).pathname.startsWith('/mindos/') } catch { return false }
    },
    /** 只放行本机后端自己，外加应用自用的 blob 与内联图片。其余一律拦掉。 */
    shouldBlockRendererRequest(value) {
      if (sameOrigin(value)) return false
      try {
        const url = new URL(value)
        if (url.protocol === 'blob:') return !value.startsWith(`blob:${origin}/`)
        if (url.protocol === 'data:') return !/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value)
        return true
      } catch { return true }
    },
  }
}

module.exports = { ENTRY_URL, INVOKE_CHANNEL, SNAPSHOT_CHANNEL, CSP,
  isEntryUrl, isTrustedSender, shouldBlockRendererRequest, createInvokeHandler, createAssetHandler,
  createLocalProfile }
