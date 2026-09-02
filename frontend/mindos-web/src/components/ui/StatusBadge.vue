<script setup lang="ts">
// 状态标签（「印」式方标）：统一文案 + 语义色。文案与语义色来自 src/shared/status.ts，不允许页面自定义。
// 层：你告诉我的=墨；资料里看到的=青绿；我推测的=朱砂虚线；你想成为的 / 知君的看法=朱砂实线。
import type { StatusMeta } from '@/shared/status'

withDefaults(
  defineProps<{
    meta: StatusMeta
    dot?: boolean
  }>(),
  {
    dot: false,
  },
)
</script>

<template>
  <span class="ws-badge" :class="`ws-badge--${meta.tone}`">
    <span v-if="dot" class="ws-badge__dot" aria-hidden="true" />
    <span>{{ meta.label }}</span>
  </span>
</template>

<style scoped>
.ws-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 7px;
  border-radius: 3px;
  border: 1px solid currentColor;
  background: transparent;
  color: var(--ws-text-color, #3c403d);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.5;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.ws-badge__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.ws-badge--neutral,
.ws-badge--muted {
  color: var(--ws-muted-color, #8b8e88);
}
.ws-badge--info,
.ws-badge--ink {
  color: var(--ws-text-primary-color, #1d211f);
}
.ws-badge--success {
  color: var(--ws-success-color, #4a7c59);
}
.ws-badge--warning {
  color: var(--ws-warning-color, #b8862b);
}
.ws-badge--danger,
.ws-badge--purple,
.ws-badge--accent {
  color: var(--ws-primary-color, #a6452e);
}
.ws-badge--guess {
  color: var(--ws-primary-color, #a6452e);
  border-style: dashed;
}
</style>
