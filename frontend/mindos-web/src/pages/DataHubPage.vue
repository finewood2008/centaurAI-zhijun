<script setup lang="ts">
// 资料与边界：导入资料、模型与隐私、回收站、知识档案、搜索的枢纽；附「知君会带走什么」的投影预览。
import { ref } from 'vue'
import { FileText, FolderOpen, Search, Settings, Trash2, ShieldCheck } from 'lucide-vue-next'
import { getProjection, type OntologyProjection } from '@/services/api'
import BaseButton from '@/components/ui/BaseButton.vue'
import { useToast } from '@/composables/useToast'

const toast = useToast()

const cards = [
  { to: '/materials', icon: FolderOpen, title: '原材料', desc: '导入文档、图片、音频；查看处理状态与原件出处' },
  { to: '/settings', icon: Settings, title: '设置（模型与隐私）', desc: '本地模型、外部模型开关与密钥、运行监控' },
  { to: '/knowledge', icon: FileText, title: '知识档案', desc: '由资料整理出的知识卡片' },
  { to: '/recycle-bin', icon: Trash2, title: '回收站', desc: '恢复或永久清除已删除的资料' },
  { to: '/search', icon: Search, title: '搜索', desc: '在本地资料里找回细节' },
]

const projection = ref<OntologyProjection | null>(null)
const projectionLoading = ref(false)
const projectionOpen = ref(false)

async function toggleProjection() {
  if (projectionOpen.value) {
    projectionOpen.value = false
    return
  }
  projectionLoading.value = true
  try {
    projection.value = await getProjection()
    projectionOpen.value = true
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error ? err.message : '投影加载失败' })
  } finally {
    projectionLoading.value = false
  }
}
</script>

<template>
  <div class="page zj-hub">
    <div class="page-head">
      <h1>资料与边界</h1>
      <p>资料从这里进来；什么能出设备，也在这里说清楚。</p>
    </div>

    <div class="zj-hub__grid">
      <RouterLink v-for="c in cards" :key="c.to" :to="c.to" class="zj-hub__card">
        <component :is="c.icon" :size="20" aria-hidden="true" />
        <span class="zj-hub__card-title">{{ c.title }}</span>
        <span class="zj-hub__card-desc">{{ c.desc }}</span>
      </RouterLink>
    </div>

    <section class="zj-hub__boundary">
      <h2><ShieldCheck :size="18" aria-hidden="true" />边界</h2>
      <p>原件不出设备。调用外部模型时，只发送完成当前回答所必需的问题和上下文片段；每一轮的出处条里都能看到送出了哪些理解和资料片段。标为敏感或受限的理解永远不会外发。</p>
      <p>下面是知君「可以带走」的那部分——只包含你已确认、且允许导出的理解。这也是其他 Agent 能读到的全部。</p>
      <BaseButton size="sm" :loading="projectionLoading" @click="toggleProjection">{{ projectionOpen ? '收起' : '查看可导出的认识' }}</BaseButton>
      <div v-if="projectionOpen && projection" class="zj-hub__projection">
        <p class="zj-hub__projection-meta">生成于 {{ projection.generatedAt }}</p>
        <pre>{{ projection.exportableMarkdown || '（还没有可导出的已确认理解）' }}</pre>
      </div>
    </section>
  </div>
</template>

<style scoped>
.zj-hub__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 28px;
}
.zj-hub__card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 18px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-primary-color, #a6452e);
  text-decoration: none;
  transition:
    border-color 0.15s,
    transform 0.15s;
}
.zj-hub__card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  transform: translateY(-1px);
}
.zj-hub__card-title {
  font-family: var(--ws-font-display, serif);
  font-size: 17px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-hub__card-desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-hub__boundary {
  max-width: 760px;
  padding: 18px 20px;
  border: 1px solid var(--ws-border-color-2, #e2ded4);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-hub__boundary h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-hub__boundary p {
  margin: 0 0 10px;
  font-size: 14px;
  line-height: 1.8;
  color: var(--ws-text-color, #3c403d);
}
.zj-hub__projection {
  margin-top: 12px;
}
.zj-hub__projection-meta {
  font-size: 12px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-hub__projection pre {
  max-height: 420px;
  overflow: auto;
  padding: 12px 14px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-card-bg, #f3efe6);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
