<script setup lang="ts">
defineProps<{ total: number; offset: number; size: number; busy: boolean }>()
defineEmits<{ (event: 'change', offset: number): void }>()
</script>

<template>
  <nav class="material-pager" aria-label="原材料分页">
    <span>共 {{ total }} 项，第 {{ Math.floor(offset / size) + 1 }} / {{ Math.max(1, Math.ceil(total / size)) }} 页</span>
    <div>
      <button type="button" :disabled="busy || offset === 0" @click="$emit('change', Math.max(0, offset - size))">上一页</button>
      <button type="button" :disabled="busy || offset + size >= total" @click="$emit('change', offset + size)">下一页</button>
    </div>
  </nav>
</template>

<style scoped>
.material-pager { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: center; padding: 14px 0; color: var(--ws-text-secondary-color, #777); font-size: 13px; }
.material-pager div { display: flex; gap: 8px; }
.material-pager button { padding: 7px 12px; border: 1px solid var(--ws-border-color, #ddd); border-radius: 8px; background: var(--ws-card-bg, #fff); color: var(--ws-text-color, #333); font: inherit; cursor: pointer; }
.material-pager button:disabled { opacity: .45; cursor: default; }
.material-pager button:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
</style>
