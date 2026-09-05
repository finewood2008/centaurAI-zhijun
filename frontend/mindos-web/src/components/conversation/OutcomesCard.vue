<script setup lang="ts">
// 「这段对话留下的」：一轮聊完后消息流底部的一张安静小卡。不是弹层，不阻断离开，没有按钮催人。
// 数据来自 GET /conversations/{id}/outcomes；全零时由父组件不渲染。
import { computed } from 'vue'
import type { ConversationOutcomes } from '@/services/api'
import { formatDay } from '@/shared/ontology'

const props = defineProps<{ outcomes: ConversationOutcomes }>()

const MAX_SHOWN = 3
const confirmed = computed(() => props.outcomes.confirmedClaims ?? [])
const shownConfirmed = computed(() => confirmed.value.slice(0, MAX_SHOWN))
const moreConfirmed = computed(() => Math.max(0, confirmed.value.length - MAX_SHOWN))
const working = computed(() => props.outcomes.workingClaims?.length ?? 0)
const commitments = computed(() => props.outcomes.commitments ?? [])
const pending = computed(() => props.outcomes.pendingJobs ?? 0)

function dueText(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.valueOf())) return ''
  // 未来的日期不用「N 天前」那套：直接写 M月D日
  if (d.valueOf() > Date.now()) return `${d.getMonth() + 1}月${d.getDate()}日`
  return formatDay(iso)
}
</script>

<template>
  <section class="zj-outcomes" data-testid="outcomes-card" aria-label="这段对话留下的">
    <h3 class="zj-outcomes__title">这段对话留下的</h3>
    <ul class="zj-outcomes__list">
      <li v-if="confirmed.length" class="zj-outcomes__item">
        <span class="zj-outcomes__label">已确认 {{ confirmed.length }} 条</span>
        <ul class="zj-outcomes__claims">
          <li v-for="c in shownConfirmed" :key="c.id">
            <RouterLink :to="{ path: '/me', query: { section: c.section, claim: c.id } }">{{ c.content }}</RouterLink>
          </li>
          <li v-if="moreConfirmed > 0" class="zj-outcomes__more">
            <RouterLink to="/me">还有 {{ moreConfirmed }} 条</RouterLink>
          </li>
        </ul>
      </li>
      <li v-if="working > 0" class="zj-outcomes__item">
        <RouterLink to="/me/inbox" class="zj-outcomes__link">等你点头 {{ working }} 条</RouterLink>
      </li>
      <li v-if="outcomes.decision" class="zj-outcomes__item">
        <RouterLink :to="{ path: '/judgments', query: { decisionId: outcomes.decision.id } }" class="zj-outcomes__link">
          判断「{{ outcomes.decision.title }}」已入簿<template v-if="dueText(outcomes.decision.reviewAt)">，回访 {{ dueText(outcomes.decision.reviewAt) }}</template>
        </RouterLink>
      </li>
      <li v-for="c in commitments" :key="c.claimId" class="zj-outcomes__item">
        <RouterLink :to="{ path: '/me', query: { section: 'matters', claim: c.claimId } }" class="zj-outcomes__link">
          承诺「{{ c.content }}」<template v-if="dueText(c.validTo)">期限 {{ dueText(c.validTo) }}</template>
        </RouterLink>
      </li>
      <li v-if="pending > 0" class="zj-outcomes__item zj-outcomes__muted">还在整理 {{ pending }} 件</li>
    </ul>
  </section>
</template>

<style scoped>
.zj-outcomes {
  max-width: 760px;
  margin: 4px 0 0 4px;
  padding: 10px 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-left: 3px solid var(--ws-border-color, #d8d3c8);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  font-size: 13px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-outcomes__title {
  margin: 0 0 6px;
  font-family: var(--ws-font-display, serif);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-outcomes__list,
.zj-outcomes__claims {
  margin: 0;
  padding: 0;
  list-style: none;
}
.zj-outcomes__list {
  display: grid;
  gap: 4px;
}
.zj-outcomes__item {
  line-height: 1.7;
}
.zj-outcomes__label {
  color: var(--ws-text-color, #3c403d);
}
.zj-outcomes__claims {
  padding-left: 1em;
}
.zj-outcomes__claims a,
.zj-outcomes__link {
  color: var(--ws-text-color, #3c403d);
  text-decoration: none;
  border-bottom: 1px dotted var(--ws-border-color, #d8d3c8);
}
.zj-outcomes__claims a:hover,
.zj-outcomes__link:hover {
  color: var(--ws-primary-color, #a6452e);
  border-bottom-color: currentColor;
}
.zj-outcomes__more a {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.zj-outcomes__muted {
  color: var(--ws-text-placeholder-color, #a3a69f);
}
</style>
