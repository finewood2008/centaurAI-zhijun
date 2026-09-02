<script setup lang="ts">
// 空状态：无数据时的统一展示。默认是一枚小印「空」，可用 slot 覆盖。
withDefaults(
  defineProps<{
    title?: string
    description?: string
    seal?: string
  }>(),
  {
    title: '暂无数据',
    description: '',
    seal: '空',
  },
)
</script>

<template>
  <div class="ws-empty">
    <span class="ws-empty__icon" aria-hidden="true">
      <slot name="icon">
        <span class="ws-empty__seal">{{ seal }}</span>
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
  color: var(--ws-text-secondary-color, #686b66);
  text-align: center;
}

.ws-empty__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ws-text-placeholder-color, #a3a69f);
}
.ws-empty__seal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: 1.5px solid var(--ws-primary-color, #a6452e);
  border-radius: 4px;
  color: var(--ws-primary-color, #a6452e);
  font-family: var(--ws-font-display, serif);
  font-size: 20px;
  opacity: 0.75;
}

.ws-empty__title {
  font-family: var(--ws-font-display, serif);
  font-size: 16px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-empty__desc {
  font-size: 13px;
  line-height: 1.6;
  max-width: 420px;
}

.ws-empty__action {
  margin-top: 6px;
}
</style>
