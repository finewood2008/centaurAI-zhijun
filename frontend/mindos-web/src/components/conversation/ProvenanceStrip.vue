<script setup lang="ts">
// 出处条：一行人话摘要 + 可展开的依据。展开即回执的可见部分（参考了哪些理解、哪些资料片段、
// 避开了几条被纠正的理解、这轮用的是本机还是外部模型）。
import { computed, ref } from 'vue'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import type { ProvenanceEvent, TurnMetaEvent } from '@/services/api'
import { formatDay, sectionLabel } from '@/shared/ontology'
import { anchorLine, pastDecisionSummary } from '@/shared/labels'
import { confirmedFraction } from '@/shared/selfmap'
import { channelLine, channelShort } from '@/shared/model'
import RingGlyph from '@/components/ui/RingGlyph.vue'
import ProvenanceGraph from '@/components/conversation/ProvenanceGraph.vue'

const props = defineProps<{
  provenance: ProvenanceEvent & { fromReceipt?: boolean }
  meta?: TurnMetaEvent | null
}>()

const open = ref(false)
// 以前记过的相似判断（旧后端没有这个字段）
const pastDecisions = computed(() => props.provenance.pastDecisions ?? [])
const fraction = computed(() => confirmedFraction(props.provenance.confirmedClaims.length, props.provenance.workingClaims.length))

const summary = computed(() => {
  const p = props.provenance
  const parts: string[] = []
  if (p.confirmedClaims.length) parts.push(`${p.confirmedClaims.length} 条你确认过的理解`)
  if (p.workingClaims.length) parts.push(`${p.workingClaims.length} 条还没点头的`)
  if (p.materials.length) parts.push(`${p.materials.length} 段资料`)
  if (p.retractedNotices > 0) parts.push(`避开了 ${p.retractedNotices} 条你纠正过的`)
  const base = parts.length ? `参考了 ${parts.join('、')}` : ''
  // 以前记过的相似判断：点名第一条（有日期就写日期），其余只写数量
  const past = pastDecisions.value
  if (past.length) {
    const first = past[0]
    const sentence = pastDecisionSummary(first, formatDay(first.createdAt), past.length - 1)
    return base ? `${base}；${sentence}` : sentence
  }
  return base || '这轮没有用到本体里的理解'
})

const channel = computed(() => channelLine(props.meta))
const channelTag = computed(() => channelShort(props.meta))
// 打底带上的原则与做法（旧后端没有这个字段）：内容从 confirmedClaims 里按 id 找，找不到就只写数量
const anchorIds = computed(() => new Set(props.provenance.anchorClaimIds ?? []))
const anchorText = computed(() => {
  const ids = anchorIds.value
  if (!ids.size) return ''
  const hits = props.provenance.confirmedClaims.filter((c) => ids.has(c.id))
  const ordered = [...hits.filter((c) => c.section === 'principles'), ...hits.filter((c) => c.section !== 'principles')]
  return anchorLine(ordered.map((c) => c.content), ids.size)
})
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
      <RingGlyph :fraction="fraction" :size="14" />
      <span class="zj-prov__summary">{{ summary }}</span>
      <span v-if="provenance.fromReceipt" class="zj-prov__receipt" title="由本轮回执还原">（回执）</span>
      <span v-if="channelTag" class="zj-prov__channel" :class="meta?.external ? 'is-external' : 'is-local'">{{ channelTag }}</span>
      <component :is="open ? ChevronUp : ChevronDown" :size="14" aria-hidden="true" />
    </button>
    <div v-if="open" class="zj-prov__body">
      <ProvenanceGraph :provenance="provenance" />
      <p v-if="channel" class="zj-prov__line">{{ channel }}</p>
      <p v-if="provenance.charterVersion" class="zj-prov__line">参考了你的人生章程（第 {{ provenance.charterVersion }} 版）。</p>
      <section v-if="provenance.confirmedClaims.length" class="zj-prov__group">
        <h4>你确认过的理解</h4>
        <ul>
          <li v-for="c in provenance.confirmedClaims" :key="c.id">
            <RouterLink :to="{ path: '/me', query: { section: c.section, claim: c.id } }">{{ c.content }}</RouterLink>
            <span class="zj-prov__tag">{{ sectionLabel(c.section) }}</span>
            <span v-if="anchorIds.has(c.id)" class="zj-seal zj-seal--muted zj-prov__anchor" title="商量、回访或深入时，这条原则或做法会一直带在身边">打底</span>
          </li>
        </ul>
      </section>
      <p v-if="anchorText" class="zj-prov__line" data-testid="provenance-anchor">
        <RouterLink :to="{ path: '/me', query: { section: 'principles' } }">{{ anchorText }}</RouterLink>
      </p>
      <section v-if="pastDecisions.length" class="zj-prov__group" data-testid="provenance-past">
        <h4>你以前记过的相似判断</h4>
        <ul>
          <li v-for="d in pastDecisions" :key="d.id">
            <RouterLink :to="{ path: '/judgments', query: { decisionId: d.id } }">{{ d.title }}</RouterLink>
            <span class="zj-prov__tag">当时选了「{{ d.choice }}」<template v-if="formatDay(d.createdAt)"> · {{ formatDay(d.createdAt) }}</template></span>
          </li>
        </ul>
      </section>
      <section v-if="provenance.workingClaims.length" class="zj-prov__group">
        <h4>还没点头的理解（知君只会以保留语气使用）</h4>
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
