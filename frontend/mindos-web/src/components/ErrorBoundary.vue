<script setup lang="ts">
// 错误边界：捕获后代组件渲染/生命周期异常，展示可恢复的错误状态，避免整页白屏。
import { onErrorCaptured, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ErrorState from '@/components/ui/ErrorState.vue'

const route = useRoute()
const message = ref('')

function reset() {
  message.value = ''
}

onErrorCaptured((err) => {
  message.value = err instanceof Error && err.message ? err.message : '页面渲染出错，请重试'
  return false
})

// 切换路由后重置错误状态，避免上一个页面的错误阻断后续页面渲染
watch(() => route.fullPath, reset)
</script>

<template>
  <ErrorState
    v-if="message"
    :message="message"
    retry-label="重新加载"
    @retry="reset"
  />
  <slot v-else />
</template>
