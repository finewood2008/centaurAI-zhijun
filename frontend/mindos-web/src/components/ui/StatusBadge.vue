<script setup lang="ts">
// 状态标签：统一文案 + 语义色。文案与语义色来自 src/shared/status.ts，不允许页面自定义。
// 用法：<StatusBadge :meta="materialStatusMeta(item.status)" />
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
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
}

.ws-badge__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

/* neutral */
.ws-badge--neutral {
  background: var(--ws-text-label-bg, #f5f7fa);
  color: var(--ws-text-secondary-color, #909399);
}

/* info：蓝色 */
.ws-badge--info {
  background: var(--ws-edit-color, rgba(0, 119, 255, 0.06));
  color: var(--ws-main-color, #1b99ff);
}

/* success：绿色 */
.ws-badge--success {
  background: var(--ws-success-color-bd, rgba(18, 205, 61, 0.06));
  color: var(--ws-success-color, #12cd3d);
}

/* warning：琥珀色 */
.ws-badge--warning {
  background: var(--ws-warning-color-bd, rgba(255, 188, 61, 0.14));
  color: #b45309;
}

/* danger：红色 */
.ws-badge--danger {
  background: var(--ws-danger-color-bd, rgba(255, 73, 24, 0.08));
  color: var(--ws-danger-color, #ff4918);
}

/* purple：紫色（归档 / 待开放） */
.ws-badge--purple {
  background: #f0e9fd;
  color: #7c4fd0;
}
</style>
