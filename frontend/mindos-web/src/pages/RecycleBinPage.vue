<script setup lang="ts">
// P15-05：回收站聚合原材料与知识卡片；恢复和永久清除都复用统一生命周期面板。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type KnowledgeCard, type UploadResult } from '@/services/api'
import LifecycleDangerPanel from '@/components/lifecycle/LifecycleDangerPanel.vue'
import { createSessionGate } from '@/composables/sessionGate'

const router = useRouter()
const materials = ref<UploadResult[]>([])
const knowledge = ref<KnowledgeCard[]>([])
const loading = ref(true)
const error = ref('')
const loadGate = createSessionGate()

async function load() {
  const requestSession = loadGate.next()
  loading.value = true; error.value = ''
  try {
    const [materialResult, knowledgeResult] = await Promise.all([
      api.listMaterials({ recycled: true }), api.listKnowledge({ recycled: true }),
    ])
    if (!loadGate.isCurrent(requestSession)) return
    materials.value = materialResult.items
    knowledge.value = knowledgeResult.items
  } catch (e) { if (loadGate.isCurrent(requestSession)) error.value = e instanceof Error ? e.message : '加载回收站失败' }
  finally { if (loadGate.isCurrent(requestSession)) loading.value = false }
}

function openMaterial(id: string) { router.push(`/materials/${id}`) }
function openKnowledge(id: string) { router.push(`/knowledge/${id}`) }
function completed() { load() }
onMounted(load)
onBeforeUnmount(() => loadGate.invalidate())
</script>

<template>
  <div class="page recycle-bin">
    <div class="page-head"><h1>回收站</h1><p>回收的资料和知识卡片不会参与默认列表、搜索、图谱与问答。永久清除不可恢复。</p></div>
    <p v-if="error" class="error-state">{{ error }}</p>
    <p v-else-if="loading" class="loading-state">正在加载回收站…</p>
    <template v-else>
      <section class="recycle-bin__section">
        <h2>原材料（{{ materials.length }}）</h2>
        <p v-if="!materials.length" class="empty-state">暂无已回收原材料。</p>
        <article v-for="item in materials" :key="item.materialId" class="recycle-bin__item">
          <button class="recycle-bin__title" type="button" @click="openMaterial(item.materialId)">{{ item.fileName }}</button>
          <span>{{ item.fileType }} · {{ item.status }}</span>
          <LifecycleDangerPanel target-type="material" :target-id="item.materialId" :target-title="item.fileName" :recycled="true" @completed="completed" />
        </article>
      </section>
      <section class="recycle-bin__section">
        <h2>知识卡片（{{ knowledge.length }}）</h2>
        <p v-if="!knowledge.length" class="empty-state">暂无已回收知识卡片。</p>
        <article v-for="item in knowledge" :key="item.knowledgeId" class="recycle-bin__item">
          <button class="recycle-bin__title" type="button" @click="openKnowledge(item.knowledgeId)">{{ item.title }}</button>
          <span>{{ item.updatedAt || '无更新时间' }}</span>
          <LifecycleDangerPanel target-type="knowledge" :target-id="item.knowledgeId" :target-title="item.title" :recycled="true" @completed="completed" />
        </article>
      </section>
    </template>
  </div>
</template>

<style scoped>
.recycle-bin__section { margin-top: 18px; }.recycle-bin__section h2 { margin: 0 0 10px; font-size: 16px; }
.recycle-bin__item { margin-top: 10px; padding: 12px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 8px; background: #fff; }
.recycle-bin__item > span { display: block; margin-top: 3px; color: #686b66; font-size: 12px; }
.recycle-bin__title { padding: 0; border: 0; background: transparent; color: var(--ws-primary-color, #a6452e); font: inherit; font-weight: 600; cursor: pointer; }
</style>
