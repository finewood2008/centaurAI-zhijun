<script setup lang="ts">
// 知君气泡边那枚淡淡的「知」印：正在生成时呼吸，其余时候安静。样式在 styles/immersive.css。
defineProps<{ breathing?: boolean }>()
</script>

<template>
  <span class="zj-seal-mark" :class="{ 'zj-seal-mark--breathing': breathing }" aria-hidden="true">知</span>
</template>
