<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { listReflections, type Reflection } from '@/services/api'
import { filterReflections, reflectionFilters } from '@/shared/reflections'
import { productScopeEpoch } from '@/shared/productScope'
import ReflectionCard from '@/components/reflection/ReflectionCard.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
const items = ref<Reflection[]>([])
const selected = ref('recent')
const busy = ref(true)
const error = ref('')
const lastReviewed = ref<string | null>(null)
const shown = computed(() => {
  const filtered = filterReflections(items.value, selected.value)
  const justReviewed = selected.value === 'recent' ? items.value.find(item => item.id === lastReviewed.value && item.status !== 'retired') : null
  return justReviewed && !filtered.some(item => item.id === justReviewed.id) ? [justReviewed, ...filtered] : filtered
})
let alive = true
let generation = 0
async function load() {
  const own = ++generation, epoch = productScopeEpoch()
  busy.value = true; error.value = ''
  try {
    const result = await listReflections()
    if (alive && own === generation && epoch === productScopeEpoch()) items.value = result.items
  } catch (err) {
    if (alive && own === generation && epoch === productScopeEpoch()) error.value = err instanceof Error ? err.message : '照见暂时没有打开'
  } finally { if (alive && own === generation) busy.value = false }
}
function updated(item: Reflection) { lastReviewed.value = item.id; items.value = items.value.map(old => old.id === item.id ? item : old) }
onMounted(load)
onBeforeUnmount(() => { alive = false; generation++ })
</script>
<template>
  <main class="reflections-page">
    <header><p class="eyebrow">知君 · 和你一起理解你</p><h1>照见</h1><p>把几段经历放在一起，留意其中的联系。每一个观察，都可以由你补充、修正。</p></header>
    <nav aria-label="照见分类"><button v-for="filter in reflectionFilters" :key="filter.id" type="button" :aria-pressed="selected === filter.id" @click="selected = filter.id; lastReviewed = null">{{ filter.label }}</button></nav>
    <p v-if="busy" role="status">正在打开照见…</p>
    <ErrorState v-else-if="error" :message="error" recover-on-reconnect @retry="load" />
    <template v-else>
      <div v-if="!shown.length" class="empty"><span aria-hidden="true">○</span><h2>{{ items.length ? '这里暂时没有照见' : '我们可以慢慢认识' }}</h2><p>{{ items.length ? '你可以切换分类，回看之前的观察与反馈。' : '先从你最近在意的事情聊起。有了足够的经历和依据，我会在合适的时候，和你核对一个观察。' }}</p><RouterLink to="/chat">最近有什么事，想一起想一想？</RouterLink></div>
      <ReflectionCard v-for="item in shown" :key="item.id" :reflection="item" @updated="updated" />
    </template>
  </main>
</template>
<style scoped>
.reflections-page { max-width:820px; margin:0 auto; padding:34px 28px 64px; color:#354033; }
.eyebrow { color:#7c8574; font-size:12px; letter-spacing:.12em; } h1 { font-size:32px; font-weight:500; margin:14px 0; } header>p:last-child { max-width:570px; color:#747d6e; line-height:1.9; font-size:14px; }
nav { display:flex; gap:8px; flex-wrap:wrap; margin:30px 0 24px; border-bottom:1px solid #e3e6dd; padding-bottom:14px; }
nav button { cursor:pointer; padding:8px 13px; border:0; background:transparent; border-radius:8px; color:#77806f; font:inherit; font-size:13px; } nav button[aria-pressed=true] { background:#eaf0e3; color:#445638; }
.empty { text-align:center; padding:55px 18px; max-width:500px; margin:auto; }.empty>span { font-size:42px; color:#9faa92; } h2 { font-size:19px; font-weight:500; }.empty p { color:#7b8273; line-height:1.9; font-size:14px; }.empty a { display:inline-block; margin-top:14px; color:#556c43; font-size:14px; }
@media(max-width:480px){.reflections-page{padding:24px 16px}nav{gap:2px}nav button{padding:8px 10px}}
</style>
