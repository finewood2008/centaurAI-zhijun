<script setup lang="ts">
// 「我」：一卷画像。全景图（紧凑）在卷首，核心画像逐行在下，逐行可确认、可撤、可补；点一条在嵌套抽屉里看依据。
// 第一次打开才读取；每完成一轮回复后理解可能有变，开着就刷新、合着就等下次打开。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  getClaim, getCoreProfile, getOntologyStats, listClaims, reviewClaim,
  type Claim, type CoreProfile, type OntologyStats, type ReviewAction, type Section,
} from '@/services/api'
import SideDrawer from '@/components/ui/SideDrawer.vue'
import SelfMap from '@/components/ontology/SelfMap.vue'
import PersonalSummary from '@/components/ontology/PersonalSummary.vue'
import ClaimCard from '@/components/ontology/ClaimCard.vue'
import { useToast } from '@/composables/useToast'
import { useActiveTurn } from '../activeTurn'
import { useDrawerLoad } from '../composables/useDrawerLoad'
import { useSheetPlacement } from '../composables/useSheetPlacement'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const router = useRouter()
const toast = useToast()
const placement = useSheetPlacement()
const turn = useActiveTurn()

const claims = ref<Claim[]>([])
const profile = ref<CoreProfile | null>(null)
const stats = ref<OntologyStats | null>(null)
const error = ref('')
const selected = ref<Claim | null>(null)
const busy = ref<Record<string, boolean>>({})
let session = 0

const REVIEW_LABEL: Record<ReviewAction, string> = {
  confirm: '已确认', partial: '已保存修正', context_only: '已限定为只适用于那件事',
  reject: '已否定，知君不会再提', defer: '先不保存', retract: '已撤回', reaffirm: '已重申',
}

function message(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

async function load() {
  const current = ++session
  error.value = ''
  const [claimsRes, profileRes, statsRes] = await Promise.allSettled([
    listClaims({ trust: ['confirmed', 'working'], limit: 500 }),
    getCoreProfile(),
    getOntologyStats(),
  ])
  if (current !== session) return
  if (claimsRes.status === 'fulfilled') claims.value = claimsRes.value.items
  else error.value = message(claimsRes.reason, '画像暂时读不到')
  // 旧盒端没有画像接口时 PersonalSummary 回退到前端分组
  profile.value = profileRes.status === 'fulfilled' ? profileRes.value : null
  stats.value = statsRes.status === 'fulfilled' ? statsRes.value : null
  if (selected.value) selected.value = claims.value.find(c => c.id === selected.value?.id) ?? selected.value
}

const { loaded, loading, reload } = useDrawerLoad(() => props.open, load, () => turn.value?.lastTurnAt.value ?? null)
const empty = computed(() => loaded.value && !claims.value.length && !profile.value?.lines.length)

async function selectClaimId(claimId: string) {
  const found = claims.value.find(c => c.id === claimId)
  if (found) { selected.value = found; return }
  try { selected.value = await getClaim(claimId) } catch (err) { toast({ type: 'error', message: message(err, '这条理解暂时打不开') }) }
}

function applyResult(claim: Claim, finalClaim: Claim) {
  const keep = finalClaim.trustState === 'working' || finalClaim.trustState === 'confirmed'
  const idx = claims.value.findIndex(c => c.id === claim.id)
  if (idx >= 0) { if (keep) claims.value.splice(idx, 1, finalClaim); else claims.value.splice(idx, 1) }
  else if (keep) claims.value.push(finalClaim)
  if (selected.value?.id === claim.id) selected.value = keep ? finalClaim : null
}

async function onReview(claim: Claim, action: ReviewAction, editedContent?: string) {
  busy.value = { ...busy.value, [claim.id]: true }
  try {
    const result = await reviewClaim(claim.id, { action, editedContent, surface: 'ontology_page' })
    applyResult(claim, result.replacedBy ?? result.claim)
    toast({ type: 'success', message: REVIEW_LABEL[action] })
    void reload()
  } catch (err) {
    toast({ type: 'error', message: message(err, '操作失败') })
  } finally {
    const next = { ...busy.value }
    delete next[claim.id]
    busy.value = next
  }
}

function onChanged(claim: Claim) {
  applyResult(claim, claim)
  void reload()
}

function browse(section: Section | 'inbox') {
  emit('close')
  void router.push(section === 'inbox' ? '/me/inbox' : { path: '/me', query: { section } })
}
</script>

<template>
  <SideDrawer :open="open" title="我 · 知君对你的理解" :placement="placement" @close="emit('close')">
    <div class="zj-drawer zj-self" data-testid="self-scroll">
      <p v-if="loading && !loaded" class="zj-drawer__quiet">正在展开画像…</p>
      <p v-else-if="error && !claims.length" class="zj-drawer__quiet" role="status">{{ error }} <button type="button" class="zj-drawer__link" @click="reload">重试</button></p>
      <p v-else-if="empty" class="zj-drawer__quiet">知君还不够了解你。多聊几句，画像会在这里慢慢展开。</p>
      <template v-else>
        <div class="zj-self__map">
          <SelfMap compact :claims="claims" :stats="stats" :selected-id="selected?.id ?? null" @select="c => (selected = c)" />
        </div>
        <PersonalSummary :claims="claims" :profile="profile" @select="c => (selected = c)" @select-claim="selectClaimId" @browse="browse" @changed="onChanged" />
      </template>
      <footer class="zj-drawer__foot">
        <RouterLink to="/me" class="zj-drawer__link" @click="emit('close')">完整档案 →</RouterLink>
      </footer>
    </div>
  </SideDrawer>
  <SideDrawer :open="!!selected" title="这条理解与依据" :placement="placement" @close="selected = null">
    <ClaimCard v-if="selected" :claim="selected" :busy="!!busy[selected.id]" show-section @review="(action, edited) => onReview(selected!, action, edited)" @updated="c => applyResult(selected!, c)" />
  </SideDrawer>
</template>
