<script setup lang="ts">
// 承载非流路由（/settings、/me、/review、/data、/materials/:id …）的宽抽屉：舞台不变，看完合上回到 /chat。
import SideDrawer from '@/components/ui/SideDrawer.vue'

defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()
</script>

<template>
  <SideDrawer :open="open" :title="title" wide @close="emit('close')">
    <div class="zj-route-page"><slot /></div>
  </SideDrawer>
</template>

<style scoped>
.zj-route-page { min-height: 100%; }
</style>
