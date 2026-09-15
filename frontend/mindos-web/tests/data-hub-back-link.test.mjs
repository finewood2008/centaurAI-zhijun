import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse, compileTemplate } from '@vue/compiler-sfc'
import { createMemoryHistory, createRouter } from 'vue-router'

const read = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')
const back = await read('components/ui/DataHubBackLink.vue')
const pages = ['RawMaterialsPage', 'SearchPage', 'SettingsPage', 'KnowledgePage', 'RecycleBinPage']

test('all five first-level modules show one fixed data hub link independently of loading state', async () => {
  for (const page of pages) {
    const source = await read(`pages/${page}.vue`)
    const template = parse(source).descriptor.template.content
    assert.equal((template.match(/<DataHubBackLink\s*\/>/g) || []).length, 1, page)
    assert.match(template, /^\s*<div[^>]*>\s*<DataHubBackLink\s*\/>/, page)
    assert.deepEqual(compileTemplate({ source: template, filename: `${page}.vue`, id: page }).errors, [])
  }
  assert.match(back, /<RouterLink[^>]+to="\/data"/)
  assert.match(back, /返回资料与边界/)
  assert.doesNotMatch(back, /router\.back|history\.back|window\.location|location\.href/)
  assert.match(back, /max-width: 100%/)
  assert.match(back, /:focus-visible/)
  for (const page of ['MaterialDetailPage', 'KnowledgeEditPage', 'ConversationPage']) {
    assert.doesNotMatch(await read(`pages/${page}.vue`), /DataHubBackLink/)
  }
})

test('fixed return works from a fresh deep link and normal router navigation respects existing leave veto', async () => {
  for (const path of ['/materials', '/search', '/settings', '/knowledge', '/recycle-bin']) {
    const router = createRouter({ history: createMemoryHistory(), routes: [path, '/data'].map(path => ({ path, component: {} })) })
    await router.push(path)
    let unsaved = true
    router.beforeEach((to, from) => from.path === path && to.path === '/data' && unsaved ? false : true)
    await router.push('/data')
    assert.equal(router.currentRoute.value.path, path)
    unsaved = false
    await router.push('/data')
    assert.equal(router.currentRoute.value.path, '/data')
  }
})
