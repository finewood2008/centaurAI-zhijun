<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { continueMatter, listMatters, type Matter } from '@/services/matters'
const router = useRouter()
const items = ref<Matter[]>([]), error = ref(''), busy = ref(''), expanded = ref(false), loaded = ref(false)
let alive = true
async function load() {
  try { const result = await listMatters('active'); if (alive) { items.value = result.items; error.value = ''; loaded.value = true } }
  catch (e) { if (alive) error.value = e instanceof Error ? e.message : '事情暂时未能读取' }
}
async function resume(item: Matter) {
  if (busy.value) return
  busy.value = item.id; error.value = ''
  try { const cid = await continueMatter(item, crypto.randomUUID()); if (alive) await router.push('/c/' + cid) }
  catch (e) { if (alive) error.value = e instanceof Error ? e.message : '暂时未能继续' }
  finally { if (alive) busy.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { alive = false })
</script>
<template>
  <section class="matters-home" aria-labelledby="matters-home-title">
    <div class="matters-head"><h2 id="matters-home-title">正在推进</h2><RouterLink :to="{ path: '/chat', query: { say: '最近我想推进的一件事是：' } }">聊一件新事情</RouterLink></div>
    <p v-if="error" class="error" role="status">事情列表暂未就绪，不影响继续聊天。<button type="button" @click="load">重新读取</button></p>
    <div v-else-if="items.length" class="matters-grid">
      <article v-for="item in expanded ? items : items.slice(0, 3)" :key="item.id"><h3>{{ item.title }}</h3><p>{{ item.nextStep || item.goal || '接着聊聊进展，或准备一份可以直接使用的文稿。' }}</p><button type="button" :disabled="!!busy" @click="resume(item)">{{ busy === item.id ? '正在打开…' : '接着推进' }} <span aria-hidden="true">→</span></button></article>
    </div>
    <p v-else-if="loaded" class="matters-empty">从眼下最想推进的一件事聊起。需要持续跟进时，在对话里打开“事情与成果”，留下一份提纲、决定或下一步。</p>
    <button v-if="items.length > 3" class="more" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起' : '查看其余 ' + (items.length - 3) + ' 件事' }}</button>
  </section>
</template>
<style scoped>
.matters-home{min-width:0;margin-bottom:24px}.matters-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:14px}.matters-head h2{font:600 22px var(--ws-font-display,serif);margin:0}.matters-head a{font-size:14px;color:var(--ws-primary-color,#a6452e);text-decoration:none}.matters-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:14px}.matters-grid article{padding:18px 20px;border:1px solid var(--ws-border-color,#d8d3c8);border-radius:12px;background:var(--ws-card-bg,#fff);min-width:0}.matters-grid h3{font-size:17px;line-height:1.6;margin:0 0 8px;overflow-wrap:anywhere}.matters-grid p{font-size:14px;line-height:1.8;color:var(--ws-text-secondary-color,#686b66);margin:0 0 14px;white-space:pre-wrap;overflow-wrap:anywhere}.matters-home button{font:inherit;font-size:14px;border:0;background:transparent;color:var(--ws-primary-color,#a6452e);cursor:pointer;padding:0}.matters-home button:disabled{opacity:.5;cursor:wait}.matters-empty{font-size:15px;line-height:1.8;padding:16px 20px;border:1px solid var(--ws-border-color-3,#ebe7de);border-radius:12px;margin:0}.more{margin-top:12px}.error{font-size:14px;color:var(--ws-text-secondary-color,#686b66)}.error button{margin-left:10px}
</style>
