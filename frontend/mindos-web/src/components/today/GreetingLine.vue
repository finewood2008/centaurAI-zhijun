<script setup lang="ts">
// 今日首屏的问候：宋体一行「称呼，M月D日 周几。」，下面一行按数据拼的灰字（模板句，不调模型）。
// 加载完成前显示两块灰条，避免称呼晚到时闪一下。
defineProps<{ line: string; summary: string; loading?: boolean }>()
</script>

<template>
  <header class="zj-greet" aria-live="polite">
    <template v-if="loading">
      <span class="zj-greet__skeleton zj-greet__skeleton--title" aria-hidden="true" />
      <span class="zj-greet__skeleton zj-greet__skeleton--line" aria-hidden="true" />
    </template>
    <template v-else>
      <h1 class="zj-greet__title" data-testid="today-greeting">{{ line }}</h1>
      <p class="zj-greet__summary" data-testid="today-summary">{{ summary }}</p>
    </template>
  </header>
</template>

<style scoped>
.zj-greet {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 4px 4px 0;
}
.zj-greet__title {
  margin: 0;
  font-family: var(--ws-font-display, serif);
  font-size: var(--ws-display-1, 26px);
  font-weight: 600;
  letter-spacing: 0.02em;
  line-height: var(--ws-lh-tight, 1.3);
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-greet__summary {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-greet__skeleton {
  display: block;
  border-radius: 3px;
  background: var(--ws-border-color-4, #f1eee6);
}
.zj-greet__skeleton--title {
  width: 40%;
  max-width: 260px;
  height: 30px;
}
.zj-greet__skeleton--line {
  width: 60%;
  max-width: 360px;
  height: 16px;
}
</style>
