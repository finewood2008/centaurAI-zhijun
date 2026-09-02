<script setup lang="ts">
// 判断的四步：记下当时 → 等结果 → 记下结果 → 复盘。用点与线表达进度，文字标签始终存在。
import { computed } from 'vue'

const props = defineProps<{ status: 'open' | 'outcome_recorded' | 'reviewed' | string }>()

const STEPS = ['记下当时', '等结果', '记下结果', '复盘'] as const

// 每一步的状态：done（实心）/ active（当前，柔和脉动）/ todo（空心）
const states = computed<Array<'done' | 'active' | 'todo'>>(() => {
  if (props.status === 'reviewed') return ['done', 'done', 'done', 'done']
  if (props.status === 'outcome_recorded') return ['done', 'done', 'done', 'active']
  return ['done', 'active', 'todo', 'todo']
})

const summary = computed(() => {
  const i = states.value.lastIndexOf('active')
  return i >= 0 ? `当前：${STEPS[i]}` : '已完成复盘'
})
</script>

<template>
  <ol class="zj-stepper" :aria-label="`判断进度，${summary}`">
    <li v-for="(label, i) in STEPS" :key="label" class="zj-stepper__step" :class="`is-${states[i]}`" :aria-current="states[i] === 'active' ? 'step' : undefined">
      <span class="zj-stepper__dot" aria-hidden="true" />
      <span class="zj-stepper__label">{{ label }}</span>
      <span v-if="i < STEPS.length - 1" class="zj-stepper__line" aria-hidden="true" />
    </li>
  </ol>
</template>

<style scoped>
.zj-stepper {
  display: flex;
  align-items: center;
  gap: 0;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}
.zj-stepper__step {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-stepper__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid var(--ws-border-color, #d8d3c8);
  background: var(--ws-body-bg, #fffcf6);
  flex: none;
}
.zj-stepper__step.is-done .zj-stepper__dot {
  border-color: #1d211f;
  background: #1d211f;
}
.zj-stepper__step.is-done {
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-stepper__step.is-active .zj-stepper__dot {
  border-color: #a6452e;
  background: #a6452e;
  box-shadow: 0 0 0 0 rgba(166, 69, 46, 0.35);
  animation: zj-step-pulse 1.8s ease-out infinite;
}
.zj-stepper__step.is-active {
  color: #a6452e;
  font-weight: 600;
}
.zj-stepper__line {
  width: 28px;
  height: 0;
  margin: 0 8px;
  border-top: 1.5px solid var(--ws-border-color, #d8d3c8);
}
.zj-stepper__step.is-done .zj-stepper__line {
  border-top-color: #1d211f;
}
@keyframes zj-step-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(166, 69, 46, 0.35);
  }
  100% {
    box-shadow: 0 0 0 8px rgba(166, 69, 46, 0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .zj-stepper__step.is-active .zj-stepper__dot {
    animation: none;
  }
}
@media (max-width: 480px) {
  .zj-stepper__line {
    width: 12px;
    margin: 0 4px;
  }
}
</style>
