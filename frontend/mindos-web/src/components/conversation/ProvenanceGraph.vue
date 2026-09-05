<script setup lang="ts">
import { computed } from 'vue'
import type { ContextItem, ProvenanceEvent } from '@/services/api'
import { contextItems, normalizeContextPlan } from '@/shared/provenanceGraph'
import { charterSourceLabel } from '@/shared/charterWorkspace'
const props = defineProps<{ provenance: ProvenanceEvent }>()
const plan = computed(() => normalizeContextPlan(props.provenance.contextPlan))
const provided = computed(() => plan.value ? contextItems(plan.value, 'providedRefs') : [])
const cited = computed(() => plan.value ? contextItems(plan.value, 'citedRefs') : [])
function sourceLink(item: ContextItem) {
  if (item.kind === 'claim') return { path: '/me', query: { claim: item.id, section: item.claim?.section } }
  if (item.kind === 'material') return { path: '/materials/' + encodeURIComponent(item.material?.materialId || item.ref?.id || item.id) }
  if (item.kind === 'decision') return { path: '/judgments', query: { decisionId: item.id } }
  return null
}
</script>
<template>
  <div v-if="plan" class="zj-context" data-testid="provenance-graph" aria-label="本轮约定、提供信息与明确引用">
    <section class="zj-context__section" data-testid="context-constraints">
      <h4>遵循的约定</h4>
      <div v-if="provenance.charterBasis?.version && provenance.charterBasis.clauseIds.length" data-testid="provenance-charter-clauses">
        <RouterLink :to="{ path: '/me/charter', query: { version: provenance.charterBasis.version } }">人生章程第 {{ provenance.charterBasis.version }} 版</RouterLink> · {{ provenance.charterBasis.clauseIds.length }} 条约定
        <details><summary>查看条款标识</summary>{{ provenance.charterBasis.clauseIds.join('、') }}</details>
      </div>
      <p v-else>本轮没有记录章程约定。资料权限仍独立检查。</p>
      <p class="zj-context__note">约定约束处理方式，不等于关于你的事实或回答引用。</p>
    </section>
    <section class="zj-context__section" data-testid="context-provided">
      <h4>提供给模型的信息 <span>{{ provided.length }} 项</span></h4>
      <p v-if="!provided.length">本轮未记录额外提供的信息条目。</p>
      <details v-for="item in provided" :key="item.citationId" class="zj-context__item">
        <summary><span class="zj-context__id">[{{ item.citationId }}]</span> {{ item.title || item.id }} <small>{{ plan.background.some(b => b.citationId === item.citationId) ? '背景' : '证据' }}</small></summary>
        <p class="zj-context__text">{{ item.text }}</p>
        <p class="zj-context__note">{{ charterSourceLabel(item.kind) }} · 版本 {{ item.version }}<template v-if="item.claim?.trustState"> · {{ item.claim.trustState === 'confirmed' ? '已确认理解' : '待核对理解' }}</template></p>
        <RouterLink v-if="sourceLink(item)" :to="sourceLink(item)!">查看原记录（可能已有新版本）</RouterLink>
      </details>
      <p class="zj-context__note">这里是实际放进本轮请求的片段，不表示完整阅读原文件，也不保证每项都影响了回答。</p>
    </section>
    <section class="zj-context__section" data-testid="context-cited">
      <h4>回答明确引用的信息 <span>{{ cited.length }} 项</span></h4>
      <ul v-if="cited.length"><li v-for="item in cited" :key="item.citationId"><span class="zj-context__id">[{{ item.citationId }}]</span> {{ item.title || item.id }} · 版本 {{ item.version }}</li></ul>
      <p v-else>这条回答没有标注可核验的来源引用；不能据此判断模型是否受到某条信息影响。</p>
      <p class="zj-context__note">只列出回答中出现、且确实提供过的引用标识。它不证明结论被证据支持，也不是因果影响或影响权重。</p>
      <p v-if="plan.citationAudit?.invalidRefs.length" class="zj-context__note">另有 {{ plan.citationAudit.invalidRefs.length }} 个无法核验的引用标识，未列入明确引用。</p>
    </section>
    <details v-if="plan.excluded.length" class="zj-context__excluded"><summary>{{ plan.excluded.length }} 项信息未纳入本轮</summary><p v-for="(item, index) in plan.excluded" :key="item.id || index">{{ item.title || '相关信息' }}：{{ item.reason }}</p></details>
    <p v-if="plan.stage === 'supplemented'" class="zj-context__note">本轮补查过一次，以上记录包含获准提供的补充信息。</p>
  </div>
  <p v-else class="zj-context__note">旧回执无法区分实际提供的信息与回答引用，不展示推定的使用关系。</p>
</template>
<style scoped>
.zj-context { font-size:12px; line-height:1.75; overflow-wrap:anywhere; }.zj-context__section { margin:14px 0; padding:0 0 12px; border-bottom:1px solid var(--ws-border-color-3); }.zj-context h4 { color:var(--ws-text-color); font-size:13px; margin:0 0 8px; }.zj-context h4 span { font-weight:400; color:var(--ws-text-secondary-color); margin-left:5px; }.zj-context p { margin:6px 0; }.zj-context__note,.zj-context small { color:var(--ws-text-secondary-color); font-size:12px; }.zj-context__item { padding:7px 0; }.zj-context summary { cursor:pointer; }.zj-context__id { font-variant-numeric:tabular-nums; color:var(--ws-primary-color); }.zj-context__text { white-space:pre-wrap; padding:10px 12px; background:var(--ws-surface-2); border-radius:8px; max-height:240px; overflow:auto; }.zj-context a { color:var(--ws-primary-color); }.zj-context ul { padding-left:18px; margin:6px 0; }.zj-context__excluded { margin:10px 0; }
</style>
