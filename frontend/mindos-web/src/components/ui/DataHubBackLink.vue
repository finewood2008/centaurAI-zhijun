<script setup lang="ts">
import { ArrowLeft } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'
</script>

<template>
  <RouterLink class="data-hub-back" to="/data">
    <ArrowLeft :size="16" aria-hidden="true" />
    <span>返回资料与边界</span>
  </RouterLink>
</template>

<style scoped>
.data-hub-back { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; min-height: 36px; margin-bottom: 12px; padding: 4px 0; box-sizing: border-box; color: var(--ws-primary-color, #a6452e); font-size: 13px; line-height: 1.5; text-decoration: none; }
.data-hub-back:hover { text-decoration: underline; }
.data-hub-back:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; border-radius: 3px; }
.data-hub-back svg { flex-shrink: 0; }
</style>
