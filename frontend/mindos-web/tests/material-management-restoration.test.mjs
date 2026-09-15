// Execute desktop routes, page setup and the real upload API with synthetic transport.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const src = fileURLToPath(new URL('../src/', import.meta.url))
const compile = source => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
function modules() {
  const cache = new Map()
  function load(filename) {
    if (!extname(filename)) filename += '.ts'
    if (filename.endsWith('.vue')) return { __esModule: true, default: { page: filename } }
    if (filename.endsWith('.json')) return JSON.parse(readFileSync(filename, 'utf8'))
    if (cache.has(filename)) return cache.get(filename).exports
    const module = { exports: {} }; cache.set(filename, module)
    const native = createRequire(filename)
    const req = name => name.startsWith('.') ? load(resolve(dirname(filename), name))
      : name.startsWith('@/') ? load(resolve(src, name.slice(2))) : native(name)
    new Function('require', 'module', 'exports', compile(readFileSync(filename, 'utf8')))(req, module, module.exports)
    return module.exports
  }
  return name => load(resolve(src, name))
}

function pageSetup(name, load) {
  const source = readFileSync(resolve(src, `pages/${name}.vue`), 'utf8')
  const exports = {}, mounts = [], cleanups = [], navigations = [], notices = []
  const req = id => {
    if (id === 'vue') return { ...Vue, onMounted: fn => mounts.push(fn), onBeforeUnmount: fn => cleanups.push(fn) }
    if (id === 'vue-router') return { onBeforeRouteLeave: () => {}, useRoute: () => ({ query: {} }), useRouter: () => ({ push: path => navigations.push(path) }) }
    if (id === '@/composables/useToast') return { useToast: () => value => notices.push(value) }
    if (id.startsWith('@/components/') || id === 'lucide-vue-next') return {}
    if (id.startsWith('@/')) return load(id.slice(2))
    throw Error(`Unexpected import: ${id}`)
  }
  new Function('require', 'exports', compile(compileScript(parse(source).descriptor, { id: name }).content))(req, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup({}, { expose() {} }))
  return { ui, mounts, navigations, notices, close() { cleanups.forEach(fn => fn()); scope.stop() } }
}

test('all management deep links load their actual page in desktop and web modes', async () => {
  const expected = {
    '/materials': 'RawMaterialsPage', '/materials/:materialId': 'MaterialDetailPage',
    '/knowledge': 'KnowledgePage', '/knowledge/new': 'KnowledgeEditPage',
    '/knowledge/:knowledgeId': 'KnowledgeEditPage', '/recycle-bin': 'RecycleBinPage',
    '/search': 'SearchPage', '/graph': 'GraphPage',
  }
  for (const desktop of [false, true]) {
    const load = modules()
    if (desktop) load('shared/productScope.ts').enableDesktopProduct()
    const routes = load('router/routes.ts').productRoutes
    for (const [path, page] of Object.entries(expected)) {
      const route = routes.find(candidate => candidate.path === path)
      assert.equal((await route.component()).default.page, resolve(src, `pages/${page}.vue`), `${desktop ? 'desktop' : 'web'} ${path}`)
    }
    assert.equal(routes.find(route => route.path === '/materials').meta.title, '原材料')
  }
})

test('desktop data hub exposes material management and knowledge navigation without changing consent state', () => {
  const load = modules()
  load('shared/productScope.ts').enableDesktopProduct()
  const page = pageSetup('DataHubPage', load)
  assert.deepEqual(page.ui.primaryCards.map(card => [card.to, card.title]), [
    ['/materials', '原材料'], ['/search', '找回资料'], ['/settings', '偏好（模型与隐私）'],
  ])
  assert.deepEqual(page.ui.moreCards.map(card => [card.to, card.title]), [
    ['/knowledge', '知识档案'], ['/recycle-bin', '回收站'],
  ])
  assert.deepEqual(page.navigations, [])
  assert.deepEqual(page.notices, [])
  assert.equal(page.ui.purgeOpen.value, false)
  page.close()
})

test('restored desktop raw-material import uses the existing scoped upload transport and progress callback', async () => {
  const load = modules(), scope = load('shared/productScope.ts')
  scope.enableDesktopProduct(); scope.setProductScope('synthetic-material-box')
  const calls = [], material = {
    materialId: 'synthetic-accepted', fileName: 'synthetic.txt', fileType: 'document', status: 'available',
    folderId: 7, folder: '合成目录', createdAt: '2026-09-14T00:00:00Z',
  }
  let page
  load('services/transport.ts').installProductTransport(async (path, init = {}) => {
    calls.push({ path, init })
    if (path === '/api/mindos/uploads') {
      assert.equal(init.method, 'POST')
      assert.ok(init.body instanceof FormData)
      assert.equal(init.body.get('file').name, 'synthetic.txt')
      assert.equal(init.body.get('folderId'), '7')
      init.onUploadProgress({ loaded: 3, total: 3, phase: 'finalizing' })
      assert.equal(page.ui.transientUploads.value[0].uploadProgress.phase, 'finalizing')
      assert.equal(page.ui.importing.value, true, 'transport completion still awaits acceptance')
      return Response.json(material)
    }
    assert.ok(path.startsWith('/api/mindos/materials?'), path)
    return Response.json({ items: [material] })
  })
  page = pageSetup('RawMaterialsPage', load)
  page.ui.selectedFolderId.value = 7
  try {
    await page.ui.importFiles([new File(['abc'], 'synthetic.txt', { type: 'text/plain' })])
    assert.equal(calls.filter(call => call.init.method === 'POST').length, 1)
    assert.equal(page.ui.items.value[0].materialId, material.materialId)
    assert.equal(page.ui.transientUploads.value.length, 0)
    assert.equal(page.ui.importing.value, false)
    assert.equal(page.notices.at(-1).type, 'success')
  } finally { page.close() }
})
