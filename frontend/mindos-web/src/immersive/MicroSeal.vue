<script setup lang="ts">
// 气泡边的小印：「记」（记下的理解）「留」（这段对话留下的）「据」（回答依据）。点开才见细节，不是徽章。
defineProps<{ kind: '记' | '留' | '据'; label: string; pressed?: boolean; lit?: boolean }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()
</script>

<template>
  <button
    type="button"
    class="zj-seal zj-microseal"
    :class="{ 'is-lit': lit, 'is-open': pressed }"
    :aria-pressed="!!pressed"
    :aria-label="label"
    :title="label"
    @click="emit('toggle')"
  >{{ kind }}</button>
</template>

<style scoped>
.zj-microseal {
  padding: 0;
  width: 22px;
  height: 22px;
  justify-content: center;
  border-radius: 3px;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: var(--ws-font-display, serif);
  font-size: 12px;
  cursor: pointer;
  transition: color 0.15s;
}
.zj-microseal.is-lit,
.zj-microseal.is-open,
.zj-microseal:hover {
  color: var(--ws-primary-color, #a6452e);
}
</style>
