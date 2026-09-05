<script setup lang="ts">
// 出处条：一行人话摘要 + 可展开的依据。展开即回执的可见部分（参考了哪些理解、哪些资料片段、
// 避开了几条被纠正的理解、这轮用的是本机还是外部模型）。
import { computed, ref } from 'vue'
import { ChevronDown, ChevronUp, FileText } from 'lucide-vue-next'
import type { ProvenanceEvent, TurnMetaEvent } from '@/services/api'
import { formatDay, sectionLabel } from '@/shared/ontology'
import { ALIGNMENT_LEVELS } from '@/shared/alignment'
import { channelShort } from '@/shared/model'
import { normalizeProvenance, provenanceCharterSummary, provenanceMemorySummary } from '@/shared/provenanceGraph'
import ProvenanceGraph from '@/components/conversation/ProvenanceGraph.vue'

const props = defineProps<{
  provenance: ProvenanceEvent & { fromReceipt?: boolean }
  meta?: TurnMetaEvent | null
}>()

const open = ref(false)
const safeProvenance = computed(() => normalizeProvenance(props.provenance))
// 以前记过的相似判断（旧后端没有这个字段）
const pastDecisions = computed(() => safeProvenance.value.pastDecisions ?? [])
const charterSummary = computed(() => provenanceCharterSummary(safeProvenance.value))

const summary = computed(() => provenanceMemorySummary(safeProvenance.value))
const lookupNotice = computed(() => safeProvenance.value.contextPlan?.stage === 'lookup_unavailable' ? safeProvenance.value.contextPlan.lookupNotice : '')
const channelTag = computed(() => channelShort(props.meta))
// 打底带上的原则与做法（旧后端没有这个字段）：内容从 confirmedClaims 里按 id 找，找不到就只写数量
const anchorIds = computed(() => new Set(safeProvenance.value.anchorClaimIds ?? []))
const anchorText = computed(() => anchorIds.value.size ? `旧回执标记了 ${anchorIds.value.size} 条打底理解，不据此推定本轮读取或引用。` : '')
</script>

<template>
  <div class="zj-prov" :class="{ 'is-open': open }">
    <button
      type="button"
      class="zj-prov__toggle"
      data-testid="provenance-toggle"
      :aria-expanded="open"
      @click="open = !open"
    >
      <FileText :size="14" aria-hidden="true" />
      <span class="zj-prov__summary">{{ summary }}</span>
      <span v-if="provenance.fromReceipt" class="zj-prov__receipt" title="由本轮回执还原">（回执）</span>
      <span v-if="channelTag" class="zj-prov__channel" :class="meta?.external ? 'is-external' : 'is-local'">{{ channelTag }}</span>
      <component :is="open ? ChevronUp : ChevronDown" :size="14" aria-hidden="true" />
    </button>
    <p v-if="lookupNotice" class="zj-prov__line zj-prov__lookup-notice" data-testid="context-lookup-notice">{{ lookupNotice }}</p>
    <p v-if="provenance.routing?.handlingNotice" class="zj-prov__line" data-testid="routing-handling-notice">{{ provenance.routing.handlingNotice }}</p>
    <div v-if="open" class="zj-prov__body">
      <p v-if="provenance.routing">{{ provenance.routing.service.external ? '在线处理' : '本地处理' }} · {{ provenance.routing.service.name }} · {{ provenance.routing.service.model }} · {{ provenance.routing.purposeLabel }}</p>
      <p v-if="provenance.routing?.defaultAuthorization">其中 {{ provenance.routing.defaultAuthorization.sourceCount }} 项来源按你开启的默认授权处理（设置第 {{ provenance.routing.defaultAuthorization.revision }} 版）。可在「模型与授权」关闭。</p>
      <p v-if="provenance.routing?.excluded.length">本轮有 {{ provenance.routing.excluded.length }} 条历史或资料未纳入：{{ [...new Set(provenance.routing.excluded.map(x => x.reason))].join('；') }}</p>
      <ProvenanceGraph v-if="safeProvenance.contextPlan" :provenance="safeProvenance" />
      <template v-else>
      <p class="zj-prov__line zj-prov__muted">这是旧格式回执。下列是当时保存的关联记录，无法准确区分哪些文本提供给了模型、哪些被回答明确引用；不会据此补写历史。</p>
      <p v-if="safeProvenance.memoryContext?.inheritedCount" class="zj-prov__line" data-testid="provenance-inherited">历史权限链关联了 {{ safeProvenance.memoryContext.inheritedCount }} 条本体理解，不代表本轮重新读取、提供或引用了原记录。</p>
      <section v-if="provenance.alignmentSources?.length" class="zj-prov__group">
        <h4>旧回执关联的自我校准</h4>
        <ul><li v-for="source in provenance.alignmentSources" :key="source.fingerprint">
          {{ source.content }} · 第 {{ source.revision }} 版 · {{ source.level == null ? '尚未校准' : ALIGNMENT_LEVELS[source.level] }}
          <span v-if="source.reason"> · {{ source.reason }}</span>
        </li></ul>
      </section>
      <p v-if="charterSummary" class="zj-prov__line"><RouterLink :to="{ path: '/me/charter', query: provenance.charterVersion ? { version: provenance.charterVersion } : {} }">{{ charterSummary }}</RouterLink>。</p>
      <section v-if="provenance.charterBasis?.version && provenance.charterBasis.clauseIds.length" class="zj-prov__group" data-testid="provenance-charter-clauses"><h4>本轮章程依据</h4><p><RouterLink :to="{ path: '/me/charter', query: { version: provenance.charterBasis.version } }">查看当时第 {{ provenance.charterBasis.version }} 版</RouterLink> · 条款 {{ provenance.charterBasis.clauseIds.join('、') }}</p><p>只记录本轮采用的约定，不表示重新确认本体或授予新的资料权限。</p></section>
      <section v-if="safeProvenance.confirmedClaims.length" class="zj-prov__group">
        <h4>旧回执关联的已确认理解</h4>
        <ul>
          <li v-for="c in safeProvenance.confirmedClaims" :key="c.id">
            <RouterLink :to="{ path: '/me', query: { section: c.section, claim: c.id } }">{{ c.content }}</RouterLink>
            <span class="zj-prov__tag">{{ sectionLabel(c.section) }}</span>
            <span v-if="anchorIds.has(c.id)" class="zj-seal zj-seal--muted zj-prov__anchor" title="当时回执保存的打底标记，不代表明确引用">旧打底标记</span>
          </li>
        </ul>
      </section>
      <p v-if="anchorText" class="zj-prov__line" data-testid="provenance-anchor">
        <RouterLink :to="{ path: '/me', query: { section: 'principles' } }">{{ anchorText }}</RouterLink>
      </p>
      <section v-if="pastDecisions.length" class="zj-prov__group" data-testid="provenance-past">
        <h4>旧回执关联的历史判断</h4>
        <ul>
          <li v-for="d in pastDecisions" :key="d.id">
            <RouterLink :to="{ path: '/judgments', query: { decisionId: d.id } }">{{ d.title }}</RouterLink>
            <span class="zj-prov__tag">当时选了「{{ d.choice }}」<template v-if="formatDay(d.createdAt)"> · {{ formatDay(d.createdAt) }}</template></span>
          </li>
        </ul>
      </section>
      <section v-if="safeProvenance.workingClaims.length" class="zj-prov__group">
        <h4>旧回执关联的待核对理解</h4>
        <ul>
          <li v-for="c in safeProvenance.workingClaims" :key="c.id">
            <RouterLink to="/me/inbox">{{ c.content }}</RouterLink>
            <span class="zj-prov__tag">{{ sectionLabel(c.section) }}</span>
          </li>
        </ul>
      </section>
      <section v-if="safeProvenance.materials.length" class="zj-prov__group">
        <h4>旧回执关联的资料</h4>
        <ul>
          <li v-for="(m, i) in safeProvenance.materials" :key="`${m.materialId}-${i}`" :id="`cite-${i + 1}`">
            <span class="zj-prov__cite">m{{ i + 1 }}</span>
            <RouterLink :to="`/materials/${encodeURIComponent(m.materialId)}`">{{ m.title || m.materialId }}</RouterLink>
          </li>
        </ul>
      </section>
      </template>
      <p class="zj-prov__line zj-prov__muted">这轮给模型的提示约 {{ provenance.promptChars }} 字。</p>
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
.zj-prov__lookup-notice {
  padding: 0 8px;
  overflow-wrap: anywhere;
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
.zj-prov__receipt {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-prov__channel {
  padding: 0 7px;
  border-radius: 3px;
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
  background: var(--ws-card-bg, #fff);
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
.zj-prov__anchor {
  margin-left: 6px;
  font-size: 11px;
  line-height: 1.4;
}
.zj-prov__cite {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
}
</style>
