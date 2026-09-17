<script setup lang="ts">
// 知君对我的理解：优先展示后端核心画像原文，也就是知君每次回答都带着的那一页，逐行可重申 / 撤回 / 看依据。
// 画像读不到（旧盒端、读取失败、还是空的）时回退到前端按分区的分组，仍只展示原话与已核对的理解。
import { computed, reactive, ref } from 'vue'
import { reviewClaim, type Claim, type CoreProfile, type CoreProfileLine, type CoreProfileSection, type ReviewAction, type Section } from '@/services/api'
import { formatDay } from '@/shared/ontology'
import { alignmentLabel } from '@/shared/alignment'
import { useToast } from '@/composables/useToast'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { ontologySummary, summaryDate, summaryStatus } from './summary'

const props = defineProps<{ claims: Claim[]; profile?: CoreProfile | null }>()
const emit = defineEmits<{
  (e: 'select', claim: Claim): void
  (e: 'select-claim', claimId: string): void
  (e: 'browse', section: Section | 'inbox'): void
  (e: 'changed', claim: Claim, action: ReviewAction): void
}>()
const toast = useToast()

// 画像分区顺序与后端一致；空分区不占位
const PROFILE_SECTIONS: ReadonlyArray<{ key: CoreProfileSection; title: string }> = [
  { key: 'who', title: '我是谁' },
  { key: 'people', title: '重要的人' },
  { key: 'matters', title: '正在做的事与承诺' },
  { key: 'principles', title: '原则' },
  { key: 'ways', title: '做法' },
  { key: 'direction', title: '方向' },
  { key: 'recent', title: '近期脉络' },
]
// 来源标签 → 方印色：亲口说的用墨色，资料用青，想成为的用朱；派生行（摘要 / 判断簿 / 事项）用默认色
const SEAL_TONE: Record<string, string> = { 你告诉我的: 'zj-seal--ink', 资料里看到的: 'zj-seal--green', 你想成为的: 'zj-seal--accent' }

const removedClaims = reactive(new Set<string>())
const busy = reactive<Record<string, boolean>>({})
const retractTarget = ref<CoreProfileLine | null>(null)

const profileLines = computed(() => (props.profile?.lines ?? []).filter((line) => !(line.claimId && removedClaims.has(line.claimId))))
const profileMode = computed(() => profileLines.value.length > 0)
const sections = computed(() =>
  PROFILE_SECTIONS.map((s) => ({ ...s, lines: profileLines.value.filter((line) => line.section === s.key) })).filter((s) => s.lines.length > 0),
)
const approxChars = computed(() => (props.profile?.text ?? '').replace(/\s+/g, '').length)
const groups = computed(() => ontologySummary(props.claims))

function sealClass(label: string): string {
  return SEAL_TONE[label] ?? ''
}

function derivedLink(line: CoreProfileLine) {
  return line.kind === 'decision' && line.decisionId ? { path: '/review', query: { decisionId: line.decisionId } } : { path: '/review' }
}

function act(line: CoreProfileLine, action: 'reaffirm' | 'retract') {
  if (!line.claimId || busy[line.claimId]) return
  if (action === 'retract') {
    retractTarget.value = line
    return
  }
  void review(line, action)
}

function confirmRetract() {
  const line = retractTarget.value
  retractTarget.value = null
  if (line) void review(line, 'retract')
}

async function review(line: CoreProfileLine, action: 'reaffirm' | 'retract') {
  const claimId = line.claimId
  if (!claimId) return
  busy[claimId] = true
  try {
    const result = await reviewClaim(claimId, { action, surface: 'ontology_page' })
    const finalClaim = result.replacedBy ?? result.claim
    if (action === 'retract') removedClaims.add(claimId)
    toast({ type: 'success', message: action === 'retract' ? '已撤回，知君不会再当作对你的认识' : '已重申' })
    emit('changed', finalClaim, action)
  } catch (err) {
    toast({ type: 'error', message: err instanceof Error && err.message ? err.message : '操作失败' })
  } finally {
    delete busy[claimId]
  }
}
</script>

<template>
  <div class="personal-summary" data-testid="personal-summary">
    <template v-if="profileMode">
      <p class="personal-summary__intro" data-testid="core-profile-note">知君每次回答都带着这一页（约 {{ approxChars }} 字），这里只列已确认且不受限的内容。每一行都可以重申、撤回，或查看依据。</p>
      <div class="personal-summary__grid" data-testid="core-profile">
        <section v-for="s in sections" :key="s.key" class="personal-summary__section" :aria-labelledby="`profile-${s.key}`">
          <header><h3 :id="`profile-${s.key}`">{{ s.title }}</h3></header>
          <ul>
            <li v-for="line in s.lines" :key="line.id" class="personal-summary__line" :data-kind="line.kind">
              <span class="zj-seal" :class="sealClass(line.label)">{{ line.label }}</span>
              <p>{{ line.text }}<small v-if="line.date"> · {{ formatDay(line.date) }}</small></p>
              <div class="personal-summary__actions">
                <template v-if="line.kind === 'claim' && line.claimId">
                  <button type="button" :disabled="!!busy[line.claimId]" title="现在还是这样，重新确认一次" @click="act(line, 'reaffirm')">还是这样</button>
                  <button type="button" :disabled="!!busy[line.claimId]" title="不再把它当作对你的认识" @click="act(line, 'retract')">不再这样了</button>
                  <button type="button" @click="emit('select-claim', line.claimId)">详情</button>
                </template>
                <RouterLink v-else :to="derivedLink(line)">去回看</RouterLink>
              </div>
            </li>
          </ul>
        </section>
      </div>
    </template>
    <template v-else>
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
    </template>
    <div class="personal-summary__more"><span>也可以按内容查看：</span><button type="button" @click="emit('browse', 'people')">重要的人</button><button type="button" @click="emit('browse', 'ways')">习惯与做事方式</button><button v-if="profileMode" type="button" @click="emit('browse', 'inbox')">等我点头的理解</button></div>
    <ConfirmDialog
      :open="!!retractTarget"
      title="不再这样了？"
      :message="retractTarget ? `知君不会再把「${retractTarget.text}」当作对你的认识，也不会再在回答里提起它。` : ''"
      confirm-text="确定"
      danger
      @confirm="confirmRetract"
      @cancel="retractTarget = null"
    />
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
.personal-summary__line { display:grid; gap:6px; padding:12px 0; justify-items:start; }
.personal-summary__line p { margin:0; font-size:15px; line-height:1.8; color:var(--ws-text-primary-color); overflow-wrap:anywhere; }
.personal-summary__line small { font-size:12px; color:var(--ws-text-secondary-color); }
.personal-summary__actions { display:flex; flex-wrap:wrap; gap:4px 16px; }
.personal-summary__actions button,.personal-summary__actions a { border:0; padding:2px 0; background:transparent; color:var(--ws-text-secondary-color); font-size:13px; text-decoration:none; border-bottom:1px dotted var(--ws-border-color); }
.personal-summary__actions button:hover,.personal-summary__actions a:hover { color:var(--ws-primary-color); border-bottom-color:currentColor; }
.personal-summary__actions button:disabled { opacity:.5; cursor:default; }
.personal-summary header button,.personal-summary__more button { border:0; padding:2px 0; color:var(--ws-primary-color); background:transparent; font-size:13px; text-align:left; }
.personal-summary button:focus-visible,.personal-summary__actions a:focus-visible { outline:2px solid var(--ws-primary-color); outline-offset:4px; border-radius:3px; }
.personal-summary__more { display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:20px; color:var(--ws-text-secondary-color); font-size:13px; }
@media(max-width:760px) { .personal-summary__grid { grid-template-columns:minmax(0,1fr); gap:12px; }.personal-summary__section { padding:18px; } }
</style>
