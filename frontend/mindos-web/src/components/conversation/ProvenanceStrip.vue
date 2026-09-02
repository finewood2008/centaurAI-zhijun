<script setup lang="ts">
// 出处条：一行摘要 + 可展开的依据清单。展开即回执的可见部分（送出了哪些理解、哪些资料片段、
// 避开了几条被纠正的理解、用的是本地还是外部模型）。
import { computed, ref } from 'vue'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import type { ProvenanceEvent, TurnMetaEvent } from '@/services/api'
import { sectionLabel } from '@/shared/ontology'

const props = defineProps<{
  provenance: ProvenanceEvent
  meta?: TurnMetaEvent | null
}>()

const open = ref(false)

const summary = computed(() => {
  const p = props.provenance
  const parts = [
    `${p.confirmedClaims.length} 条已确认`,
    `${p.workingClaims.length} 条工作理解`,
    `${p.materials.length} 段资料`,
  ]
  if (p.retractedNotices > 0) parts.push(`避开 ${p.retractedNotices} 条已纠正`)
  return parts.join(' · ')
})

const channel = computed(() => {
  if (!props.meta) return ''
  if (props.meta.provider === 'fake') return '演示模型，未调用真实模型'
  return props.meta.external
    ? `外部模型「${props.meta.model}」：本轮问题与上述片段已发送至外部服务`
    : `本地模型「${props.meta.model}」：数据未离开本机`
})
</script>

<template>
  <div class="zj-prov" :class="{ 'is-open': open }">
    <button
      type="button"
      class="zj-prov__toggle"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="zj-prov__lead">依据</span>
      <span class="zj-prov__summary">{{ summary }}</span>
      <span v-if="channel" class="zj-prov__channel" :class="meta?.external ? 'is-external' : 'is-local'">{{ meta?.external ? '外部模型' : (meta?.provider === 'fake' ? '演示模型' : '本地模型') }}</span>
      <component :is="open ? ChevronUp : ChevronDown" :size="14" aria-hidden="true" />
    </button>
    <div v-if="open" class="zj-prov__body">
      <p v-if="channel" class="zj-prov__line">{{ channel }}</p>
      <p v-if="provenance.charterVersion" class="zj-prov__line">参考了人生章程第 {{ provenance.charterVersion }} 版。</p>
      <section v-if="provenance.confirmedClaims.length" class="zj-prov__group">
        <h4>已确认的理解</h4>
        <ul>
          <li v-for="c in provenance.confirmedClaims" :key="c.id">
            <RouterLink :to="{ path: '/me', query: { section: c.section } }">{{ c.content }}</RouterLink>
            <span class="zj-prov__tag">{{ sectionLabel(c.section) }}</span>
          </li>
        </ul>
      </section>
      <section v-if="provenance.workingClaims.length" class="zj-prov__group">
        <h4>还没确认的工作理解（知君只会以保留语气使用）</h4>
        <ul>
          <li v-for="c in provenance.workingClaims" :key="c.id">
            <RouterLink to="/me/inbox">{{ c.content }}</RouterLink>
            <span class="zj-prov__tag">{{ sectionLabel(c.section) }}</span>
          </li>
        </ul>
      </section>
      <section v-if="provenance.materials.length" class="zj-prov__group">
        <h4>资料片段</h4>
        <ul>
          <li v-for="(m, i) in provenance.materials" :key="`${m.materialId}-${i}`" :id="`cite-${i + 1}`">
            <span class="zj-prov__cite">m{{ i + 1 }}</span>
            <RouterLink :to="`/materials/${encodeURIComponent(m.materialId)}`">{{ m.title || m.materialId }}</RouterLink>
          </li>
        </ul>
      </section>
      <p class="zj-prov__line zj-prov__muted">本轮提示词约 {{ provenance.promptChars }} 字。</p>
    </div>
  </div>
</template>

<style scoped>
.zj-prov {
  max-width: 760px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-prov__toggle {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-prov__toggle:hover {
  border-color: var(--ws-border-color, #d8d3c8);
}
.zj-prov__lead {
  font-family: var(--ws-font-display, serif);
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-prov__channel {
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
.zj-prov__channel.is-external {
  color: var(--ws-warning-color, #b8862b);
  border-color: var(--ws-warning-color, #b8862b);
}
.zj-prov__body {
  margin-top: 6px;
  padding: 10px 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-prov__line {
  margin: 0 0 6px;
  line-height: 1.6;
}
.zj-prov__muted {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-prov__group {
  margin: 6px 0 10px;
}
.zj-prov__group h4 {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-color, #3c403d);
}
.zj-prov__group ul {
  margin: 0;
  padding-left: 1.2em;
}
.zj-prov__group li {
  line-height: 1.7;
}
.zj-prov__tag {
  margin-left: 6px;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-prov__cite {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
</style>
