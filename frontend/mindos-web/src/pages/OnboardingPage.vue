<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createConversation, getOnboardingProgress, updateOnboarding, type OnboardingProgress } from '@/services/api'
const router = useRouter()
const progress = ref<OnboardingProgress | null>(null)
const error = ref('')
const loading = ref(true)
const busy = ref(false)
let alive = true
let readSequence = 0
let continuedConversationId: string | null = null
onBeforeUnmount(() => { alive = false; readSequence++ })
async function load() {
  if (busy.value) return
  const ticket = ++readSequence
  loading.value = true; error.value = ''
  try { const value = await getOnboardingProgress(); if (alive && ticket === readSequence) progress.value = value }
  catch (e) { if (alive && ticket === readSequence) error.value = e instanceof Error ? e.message : '暂时无法读取进度' }
  finally { if (alive && ticket === readSequence) loading.value = false }
}
async function continueChat() {
  if (busy.value || loading.value || !progress.value) return
  busy.value = true; readSequence++; error.value = ''
  try {
    if (progress.value?.state === 'ready' && !progress.value.conversationId) {
      if (!continuedConversationId) continuedConversationId = (await createConversation({ mode: 'chat', title: '继续认识我' })).id
      if (!alive) return
      await router.push(`/c/${continuedConversationId}?charter=1`)
      return
    }
    if (!progress.value?.conversationId) {
      const value = await updateOnboarding('start')
      if (!alive) return
      progress.value = value
    }
    if (!progress.value.conversationId) throw new Error('尚未准备好初始化对话，请重试。')
    const prefix = progress.value?.state === 'ready' ? '/c' : '/onboarding/c'
    await router.push(prefix + '/' + progress.value.conversationId + '?charter=1')
  } catch (e) { if (alive) error.value = e instanceof Error ? e.message : '暂时无法继续' }
  finally { if (alive) busy.value = false }
}
async function finish() {
  if (busy.value || loading.value || !progress.value) return
  busy.value = true; readSequence++; error.value = ''
  try { await updateOnboarding('finish'); if (alive) await router.push('/chat') }
  catch (e) { if (alive) error.value = e instanceof Error ? e.message : '暂时无法完成' }
  finally { if (alive) busy.value = false }
}
onMounted(load)
</script>
<template>
  <main class="onboarding-summary">
    <h1>{{ progress?.state === 'ready' ? '已经可以开始使用知君' : '先形成起点，以后继续完善' }}</h1>
    <p>先聊眼下的处境与期待，不必回答固定数量的问题，也不必先导入资料。人生章程可以另行主动建立，确认后保持稳定。</p>
    <p v-if="loading" role="status">正在读取初始化进度…</p>
    <p v-if="error" role="alert">{{ error }} <button :disabled="busy || loading" @click="load">重新读取</button></p>
    <div class="actions"><button :disabled="busy || loading || !progress" @click="continueChat">{{ progress?.state === 'ready' ? '继续认识我' : '回到对话，查看小结' }}</button><button :disabled="busy || loading || !progress" @click="finish">先使用，稍后核对</button></div>
    <section><h2>以后按需要补充</h2><p>这些不是初始化关卡，模型配置和资料授权仍由你决定。</p>
      <div class="actions"><RouterLink to="/chat">聊一件眼下的事</RouterLink><RouterLink to="/me/charter">建立人生章程</RouterLink><RouterLink to="/judgments">我的判断</RouterLink><RouterLink to="/data">带入资料与持续来源</RouterLink><RouterLink to="/settings">模型与偏好</RouterLink><RouterLink to="/">今日来信</RouterLink></div>
    </section>
  </main>
</template>
<style scoped>
.onboarding-summary { max-width:760px; margin:32px auto; padding:24px; line-height:1.8; }.onboarding-summary h1 { font-size:26px; }.onboarding-summary section { margin-top:32px; }.actions { display:flex; flex-wrap:wrap; gap:12px; margin:20px 0; }.actions button,.actions a { font:inherit; color:inherit; padding:10px 16px; background:transparent; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:10px; cursor:pointer; text-decoration:none; }
</style>
