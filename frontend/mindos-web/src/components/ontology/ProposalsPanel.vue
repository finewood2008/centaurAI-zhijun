<script setup lang="ts">
// 需要你裁决：实体合并候选（合并 / 不是同一个）与理解矛盾对（留左边 / 留右边 / 两条都对）。
// 整合器绝不自动合并或改动已确认理解，这里是唯一的人工裁决入口；「现在整理一次」立即跑一遍整合器。
import { onMounted, reactive, ref } from 'vue'
import {
  ApiError,
  consolidateNow,
  getProposals,
  resolveConflict,
  resolveMergeProposal,
  type Conflict,
  type MergeProposal,
} from '@/services/api'
import { useToast } from '@/composables/useToast'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { layerMeta, sectionLabel, trustMeta } from '@/shared/ontology'
import { conflictTitle, mergeLabel } from '@/shared/proposals'

const emit = defineEmits<{ (e: 'changed'): void }>()
const toast = useToast()

const merges = ref<MergeProposal[]>([])
const conflicts = ref<Conflict[]>([])
const loading = ref(true)
const error = ref('')
const consolidating = ref(false)
const busy = reactive<Record<string, boolean>>({})

function friendly(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.status === 409) return '这一条已经处理过了，刷新后再看。'
  return err instanceof Error && err.message ? err.message : fallback
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await getProposals()
    merges.value = res.merges
    conflicts.value = res.conflicts
  } catch (err) {
    error.value = friendly(err, '裁决列表加载失败')
  } finally {
    loading.value = false
  }
}

async function onMerge(p: MergeProposal, accept: boolean) {
  busy[p.id] = true
  try {
    await resolveMergeProposal(p.id, accept)
    merges.value = merges.value.filter((m) => m.id !== p.id)
    toast({ type: 'success', message: accept ? `已把「${p.fromName || ''}」并入「${p.intoName || ''}」` : '已记下：不是同一个' })
    emit('changed')
  } catch (err) {
    toast({ type: 'error', message: friendly(err, '操作失败') })
  } finally {
    delete busy[p.id]
  }
}

async function onConflict(c: Conflict, keep: 'a' | 'b' | 'both') {
  busy[c.id] = true
  try {
    await resolveConflict(c.id, keep)
    conflicts.value = conflicts.value.filter((x) => x.id !== c.id)
    const label = keep === 'both' ? '两条都保留' : keep === 'a' ? '保留左边，另一条已撤回' : '保留右边，另一条已撤回'
    toast({ type: 'success', message: label })
    emit('changed')
  } catch (err) {
    toast({ type: 'error', message: friendly(err, '操作失败') })
  } finally {
    delete busy[c.id]
  }
}

async function runConsolidate() {
  consolidating.value = true
  try {
    const r = await consolidateNow()
    await load()
    toast({
      type: 'info',
      message: `整理完成：合并候选 ${r.mergeProposals}，矛盾 ${r.conflicts}，张力 ${r.tensions}，多处提到 ${r.promoted}，并入 ${r.merged}，标记矛盾 ${r.challenged}`,
    })
    emit('changed')
  } catch (err) {
    toast({ type: 'error', message: friendly(err, '整理失败') })
  } finally {
    consolidating.value = false
  }
}

onMounted(() => {
  void load()
})

defineExpose({ reload: load })
</script>

<template>
  <div class="zj-props">
    <div class="zj-props__toolbar">
      <button type="button" class="zj-props__consolidate" :disabled="consolidating" @click="runConsolidate">
        {{ consolidating ? '整理中…' : '现在整理一次' }}
      </button>
    </div>

    <div v-if="loading" class="loading-state">正在读取…</div>
    <ErrorState v-else-if="error" :message="error" @retry="load" />
    <EmptyState
      v-else-if="!merges.length && !conflicts.length"
      title="暂时没有需要你裁决的"
      description="知君每天整理一次本体；发现两个名字可能是同一个人、或两条理解互相矛盾时，会放到这里等你定。"
    />
    <template v-else>
      <section v-if="merges.length" class="zj-props__group">
        <h3>实体合并候选</h3>
        <article v-for="p in merges" :key="p.id" class="zj-props__merge" :aria-busy="busy[p.id] || undefined">
          <p class="zj-props__merge-text">{{ mergeLabel(p) }}</p>
          <div class="zj-props__actions">
            <button type="button" class="zj-props__btn is-primary" :disabled="busy[p.id]" @click="onMerge(p, true)">合并</button>
            <button type="button" class="zj-props__btn" :disabled="busy[p.id]" @click="onMerge(p, false)">不是同一个</button>
          </div>
        </article>
      </section>

      <section v-if="conflicts.length" class="zj-props__group">
        <h3>理解矛盾</h3>
        <article v-for="c in conflicts" :key="c.id" class="zj-props__conflict" :aria-busy="busy[c.id] || undefined">
          <header class="zj-props__conflict-head">
            <strong>{{ conflictTitle(c.kind) }}</strong>
            <span v-if="c.note" class="zj-props__note">{{ c.note }}</span>
          </header>
          <div class="zj-props__pair">
            <div v-for="side in (['a', 'b'] as const)" :key="side" class="zj-props__side">
              <div class="zj-props__side-head">
                <StatusBadge :meta="layerMeta((side === 'a' ? c.claimA : c.claimB).layer)" />
                <StatusBadge :meta="trustMeta((side === 'a' ? c.claimA : c.claimB).trustState)" />
                <span class="zj-props__section">{{ sectionLabel((side === 'a' ? c.claimA : c.claimB).section) }}</span>
              </div>
              <p class="zj-props__content">{{ (side === 'a' ? c.claimA : c.claimB).content }}</p>
            </div>
          </div>
          <div class="zj-props__actions">
            <button type="button" class="zj-props__btn" :disabled="busy[c.id]" @click="onConflict(c, 'a')">留左边</button>
            <button type="button" class="zj-props__btn" :disabled="busy[c.id]" @click="onConflict(c, 'b')">留右边</button>
            <button type="button" class="zj-props__btn is-primary" :disabled="busy[c.id]" @click="onConflict(c, 'both')">两条都对</button>
          </div>
        </article>
      </section>
    </template>
  </div>
</template>

<style scoped>
.zj-props__toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 10px;
}
.zj-props__consolidate {
  border: none;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.zj-props__consolidate:hover:not(:disabled) {
  color: var(--ws-primary-color, #a6452e);
}
.zj-props__group {
  margin-bottom: 20px;
}
.zj-props__group h3 {
  margin: 0 0 10px;
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-props__merge,
.zj-props__conflict {
  padding: 14px 16px;
  margin-bottom: 10px;
  border: 1px dashed var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-body-bg, #fffcf6);
}
.zj-props__merge {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.zj-props__merge-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-props__conflict-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 14px;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-props__note {
  font-size: 12px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-props__pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 10px;
}
.zj-props__side {
  padding: 10px 12px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-surface-2, #fbf8f1);
}
.zj-props__side-head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 12px;
}
.zj-props__section {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-props__content {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-props__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.zj-props__btn {
  padding: 4px 12px;
  border: 1px solid var(--ws-border-color, #d8d3c8);
  border-radius: 999px;
  background: var(--ws-body-bg, #fffcf6);
  color: var(--ws-text-color, #3c403d);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.zj-props__btn:hover:not(:disabled) {
  border-color: var(--ws-primary-color, #a6452e);
}
.zj-props__btn.is-primary {
  background: var(--ws-primary-color, #a6452e);
  border-color: var(--ws-primary-color, #a6452e);
  color: var(--ws-white, #fff);
}
.zj-props__btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
@media (max-width: 767px) {
  .zj-props__pair {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
