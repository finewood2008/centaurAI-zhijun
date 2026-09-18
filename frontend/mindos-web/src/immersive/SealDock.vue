<script setup lang="ts">
// 右上角四枚极小的印：我 · 昔 · 案 · 偏。每一枚打开一个抽屉（偏 = /settings 路由）；
// 「案」只在此刻有相关对象时才亮（useDeskObjects 的 deskCount > 0）。印钮用全称做 aria-label。
import { useDeskObjects } from './composables/useDeskObjects'

export type SealId = 'self' | 'past' | 'desk' | 'prefs'
const SEALS: ReadonlyArray<{ id: SealId; glyph: string; label: string }> = [
  { id: 'self', glyph: '我', label: '我 · 核心画像' },
  { id: 'past', glyph: '昔', label: '昔 · 判断与回看' },
  { id: 'desk', glyph: '案', label: '案头' },
  { id: 'prefs', glyph: '偏', label: '偏好' },
]
withDefaults(defineProps<{ active?: SealId | null }>(), { active: null })
const emit = defineEmits<{ (e: 'select', id: SealId): void }>()
const { deskCount } = useDeskObjects()
</script>

<template>
  <div class="zj-dock" role="toolbar" aria-label="印">
    <button
      v-for="seal in SEALS"
      :key="seal.id"
      type="button"
      class="zj-dock__seal"
      :class="{ 'is-lit': seal.id === 'desk' && deskCount > 0 }"
      :data-seal="seal.id"
      :aria-label="seal.label"
      :title="seal.label"
      aria-haspopup="dialog"
      :aria-expanded="active === seal.id"
      @click="emit('select', seal.id)"
    >{{ seal.glyph }}</button>
  </div>
</template>
