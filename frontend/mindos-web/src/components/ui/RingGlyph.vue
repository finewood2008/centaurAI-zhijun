<script setup lang="ts">
// 小环：实心弧占比 = 已确认 /（已确认 + 待确认）。0 → 空心。装饰性，含义由 title / 文字提供。
import { computed } from 'vue'

const props = withDefaults(defineProps<{ fraction: number; size?: number; title?: string }>(), { size: 14, title: '' })

const r = computed(() => (props.size - 3) / 2)
const c = computed(() => 2 * Math.PI * r.value)
const dash = computed(() => `${(Math.max(0, Math.min(1, props.fraction)) * c.value).toFixed(2)} ${c.value.toFixed(2)}`)
</script>

<template>
  <svg :width="size" :height="size" :viewBox="`0 0 ${size} ${size}`" class="zj-ring" aria-hidden="true">
    <title v-if="title">{{ title }}</title>
    <circle :cx="size / 2" :cy="size / 2" :r="r" fill="none" stroke="#D8D3C8" stroke-width="1.5" />
    <circle
      :cx="size / 2"
      :cy="size / 2"
      :r="r"
      fill="none"
      stroke="#A6452E"
      stroke-width="1.5"
      stroke-linecap="round"
      :stroke-dasharray="dash"
      :transform="`rotate(-90 ${size / 2} ${size / 2})`"
    />
  </svg>
</template>

<style scoped>
.zj-ring {
  display: inline-block;
  vertical-align: middle;
  flex: none;
}
</style>
