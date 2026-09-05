<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>
<template>
  <nav class="self-sections" aria-label="我的本体板块">
    <RouterLink to="/me" :class="{ active: route.path !== '/me/charter' }" :aria-current="route.path !== '/me/charter' ? 'page' : undefined">本体理解</RouterLink>
    <RouterLink to="/me/charter" :class="{ active: route.path === '/me/charter' }" :aria-current="route.path === '/me/charter' ? 'page' : undefined">人生章程</RouterLink>
  </nav>
</template>
<style scoped>
.self-sections { display:flex; flex-wrap:wrap; gap:20px; border-bottom:1px solid var(--ws-border-color-3); margin:0 0 22px; }
.self-sections a { padding:8px 2px 12px; color:var(--ws-text-secondary-color); text-decoration:none; font-size:14px; border-bottom:2px solid transparent; }
.self-sections a.active { color:var(--ws-primary-color); border-bottom-color:currentColor; }
.self-sections a:focus-visible { outline:2px solid var(--ws-primary-color); outline-offset:3px; }
</style>
