<script setup lang="ts">
import { computed } from 'vue'
import type { Claim, Section } from '@/services/api'
import { formatDay } from '@/shared/ontology'
import { alignmentLabel } from '@/shared/alignment'
import { ontologySummary, summaryDate, summaryStatus } from './summary'

const props = defineProps<{ claims: Claim[] }>()
const emit = defineEmits<{ (e: 'select', claim: Claim): void; (e: 'browse', section: Section | 'inbox'): void }>()
const groups = computed(() => ontologySummary(props.claims))
</script>

<template>
  <div class="personal-summary" data-testid="personal-summary">
    <p class="personal-summary__intro">这是你留下的原话与已核对的理解。点开任何一条，都可以查看依据或修正。</p>
    <div class="personal-summary__grid">
      <section v-for="group in groups" :key="group.key" class="personal-summary__section" :aria-labelledby="`summary-${group.key}`">
        <header><h3 :id="`summary-${group.key}`">{{ group.title }}</h3><button v-if="group.key !== 'recent' && group.total > 3" type="button" @click="emit('browse', group.section)">查看全部 {{ group.total }} 条</button></header>
        <p class="personal-summary__description">{{ group.description }}</p>
        <ul v-if="group.items.length">
          <li v-for="claim in group.items" :key="claim.id">
            <button type="button" class="personal-summary__claim" @click="emit('select', claim)">
              <span>{{ claim.content }}</span>
              <small>{{ summaryStatus(claim) }}<template v-if="claim.trustState === 'confirmed' && claim.selfAlignment?.level != null"> · {{ alignmentLabel(claim) }}</template><template v-if="group.key === 'recent'"> · {{ formatDay(summaryDate(claim)) }}</template></small>
            </button>
          </li>
        </ul>
        <p v-else class="personal-summary__empty">{{ group.key === 'uncertain' ? '暂时没有需要你核对的内容。' : '还没有适合展示的记录，可以在日常对话里慢慢补充。' }}</p>
      </section>
    </div>
    <div class="personal-summary__more"><span>也可以按内容查看：</span><button type="button" @click="emit('browse', 'people')">重要的人</button><button type="button" @click="emit('browse', 'ways')">习惯与做事方式</button></div>
  </div>
</template>

<style scoped>
.personal-summary { min-width:0; }
.personal-summary__intro { margin:0 0 20px; color:var(--ws-text-secondary-color); font-size:14px; line-height:1.75; }
.personal-summary__grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }
.personal-summary__section { min-width:0; padding:20px 22px; border:1px solid var(--ws-border-color-3); border-radius:10px; background:var(--ws-card-bg); }
.personal-summary__section header { display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:8px; }
.personal-summary h3 { margin:0; font:600 19px/1.5 var(--ws-font-display); color:var(--ws-text-primary-color); }
.personal-summary__description,.personal-summary__empty { margin:8px 0 0; color:var(--ws-text-secondary-color); font-size:13px; line-height:1.7; }
.personal-summary__empty { padding:12px 0; }
.personal-summary ul { list-style:none; padding:0; margin:12px 0 0; }
.personal-summary li + li { border-top:1px solid var(--ws-border-color-3); }
.personal-summary button { font:inherit; cursor:pointer; }
.personal-summary__claim { display:grid; gap:6px; width:100%; padding:12px 0; border:0; background:transparent; color:var(--ws-text-primary-color); text-align:left; line-height:1.8; overflow-wrap:anywhere; }
.personal-summary__claim > span { font-size:15px; }
.personal-summary__claim small { font-size:12px; color:var(--ws-text-secondary-color); }
.personal-summary__claim:hover > span { color:var(--ws-primary-color); }
.personal-summary header button,.personal-summary__more button { border:0; padding:2px 0; color:var(--ws-primary-color); background:transparent; font-size:13px; text-align:left; }
.personal-summary button:focus-visible { outline:2px solid var(--ws-primary-color); outline-offset:4px; border-radius:3px; }
.personal-summary__more { display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:20px; color:var(--ws-text-secondary-color); font-size:13px; }
@media(max-width:760px) { .personal-summary__grid { grid-template-columns:minmax(0,1fr); gap:12px; }.personal-summary__section { padding:18px; } }
</style>
