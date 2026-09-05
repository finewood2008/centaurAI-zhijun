<script setup lang="ts">
// Local visual regression only: real components, synthetic claims, no API or model calls.
import { ref } from 'vue'
import ClaimCandidateChip from '../../src/components/conversation/ClaimCandidateChip.vue'
import SideDrawer from '../../src/components/ui/SideDrawer.vue'
const drawer = ref(false)
const selection = ref('尚未操作')
const claim = (id: number) => ({
  id: `synthetic-${id}`, layer: 'self_declared', section: 'ways',
  content: `演示记录 ${id}：我希望充分理解情况之后再决定下一步。`,
  evidence: [{ quote: '这是用于检查菜单位置的合成文字，不会保存到资料库。' }],
})
</script>
<template>
  <main class="fixture">
    <header><h1>菜单边界回归（合成数据）</h1><button @click="drawer = true">检查抽屉内菜单</button></header>
    <section class="fixture-stream" aria-label="演示消息">
      <p>模拟具有裁切边界的对话正文：向下滚动检查底部卡片。</p>
      <ClaimCandidateChip v-for="n in 8" :key="n" :claim="claim(n) as any" @review="selection = $event" />
    </section>
    <footer><textarea aria-label="演示输入框" placeholder="输入框应保持独立，不遮挡菜单" /><output>{{ selection }}</output></footer>
    <SideDrawer :open="drawer" title="演示抽屉" @close="drawer = false">
      <ClaimCandidateChip :claim="claim(9) as any" @review="selection = $event" />
    </SideDrawer>
  </main>
</template>
<style scoped>
.fixture { width:min(880px,calc(100% - 32px)); height:calc(100dvh - 32px); margin:16px auto; display:flex; flex-direction:column; gap:12px; }
header { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }
h1 { margin:0; font-size:20px; }
button { font:inherit; }
.fixture-stream { flex:1; min-height:0; overflow-y:auto; padding:4px 4px 24px; display:flex; flex-direction:column; gap:14px; border:1px solid #d8d3c8; }
.fixture-stream > * { flex-shrink:0; }
footer { flex-shrink:0; padding:12px; border:1px solid #d8d3c8; border-radius:8px; background:white; }
textarea { width:100%; min-height:64px; font:inherit; resize:vertical; }
output { display:block; font-size:12px; }
</style>
