<script setup lang="ts">
// 空状态：无数据时的统一展示。图标可用 slot 覆盖（默认 Inbox）。
import { Inbox } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    title?: string
    description?: string
  }>(),
  {
    title: '暂无数据',
    description: '',
  },
)
</script>

<template>
  <div class="ws-empty">
    <span class="ws-empty__icon" aria-hidden="true">
      <slot name="icon">
        <Inbox :size="34" />
      </slot>
    </span>
    <div class="ws-empty__title">{{ title }}</div>
    <div v-if="description" class="ws-empty__desc">{{ description }}</div>
    <div v-if="$slots.action" class="ws-empty__action">
      <slot name="action" />
    </div>
  </div>
</template>

<style scoped>
.ws-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 48px 20px;
  color: var(--ws-text-secondary-color, #909399);
  text-align: center;
}

.ws-empty__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ws-text-placeholder-color, #c0c4cc);
}

.ws-empty__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #303133);
}

.ws-empty__desc {
  font-size: 12px;
  line-height: 1.6;
  max-width: 420px;
}

.ws-empty__action {
  margin-top: 6px;
}
</style>
