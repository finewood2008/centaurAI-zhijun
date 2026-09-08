'use strict'
const { realpath, readFile } = require('node:fs/promises')
const path = require('node:path')
const ENTRY_URL = 'zhijun://desktop/desktop.html'
const INVOKE_CHANNEL = 'zhijun:invoke'
const SNAPSHOT_CHANNEL = 'zhijun:snapshot'
const OPERATIONS = new Set(['getSnapshot', 'getRememberedLogin', 'beginSignIn', 'signInWithPassword', 'signInWithSavedPassword', 'listDevices', 'connect',
  'disconnect', 'signOut', 'materials.list', 'cancelRead',
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
    if (operation === 'product.requestMicrophone' && !contents.isFocused()) return denied(runtime)
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
module.exports = { ENTRY_URL, INVOKE_CHANNEL, SNAPSHOT_CHANNEL, CSP,
  isEntryUrl, isTrustedSender, shouldBlockRendererRequest, createInvokeHandler, createAssetHandler }
