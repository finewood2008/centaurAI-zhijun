<script setup lang="ts">
const emit = defineEmits<{ (e: 'count', n: number): void }>()
// 「知君想跟你聊的」：复用对话页的提醒条，今日页全部展开（最多 3 条）。
// 提醒条自己拉数据；它回报还剩几条，为零时整块不显示（v-show 是为了让它保持挂载、能加载）。
import { ref } from 'vue'
import NudgeStrip from '@/components/conversation/NudgeStrip.vue'

const count = ref(0)
</script>

<template>
  <section v-show="count > 0" class="zj-today-section" data-testid="today-nudges" aria-label="知君想跟你聊的">
    <h2 class="zj-today-section__title">知君想跟你聊的</h2>
    <NudgeStrip show-all @count="(n) => { count = n; emit('count', n) }" />
  </section>
</template>
