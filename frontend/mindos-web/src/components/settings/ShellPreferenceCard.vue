<script setup lang="ts">
// 界面与节奏两个本机偏好：经典 / 沉浸（App.vue 立即响应，不用刷新）与回复节奏（按段落浮现）。
// 只读写 localStorage，挂载时不联网。不用 switch 角色：设置页的唯一开关留给记忆整理（settings-ontology.e2e 只认一个）。
import { immersiveShell, setImmersiveShell } from '@/immersive/shellPreference'
import { pacedReplyEnabled, setPacedReply } from '@/immersive/pacedReplyPreference'
</script>

<template>
  <section class="shell-pref" aria-labelledby="shell-pref-title" data-testid="shell-preference">
    <header class="shell-pref__head">
      <h2 id="shell-pref-title">界面与节奏</h2>
      <p>只保存在这台设备上，随时可以改回来。</p>
    </header>
    <div class="shell-pref__row">
      <div class="shell-pref__copy">
        <strong>界面</strong>
        <span>沉浸：没有导航，只有一条按天分节的对话，其余都在四枚印后面。经典：侧栏与分页。</span>
      </div>
      <div class="shell-pref__choices" role="group" aria-label="界面">
        <button type="button" :aria-pressed="!immersiveShell" @click="setImmersiveShell(false)">经典</button>
        <button type="button" :aria-pressed="immersiveShell" @click="setImmersiveShell(true)">沉浸</button>
      </div>
    </div>
    <div class="shell-pref__row">
      <div class="shell-pref__copy">
        <strong>回复节奏</strong>
        <span>知君的回复按段落浮现，段间稍作停顿；关闭后整段直接出现。系统偏好减少动效时自动关闭。</span>
      </div>
      <button type="button" class="shell-pref__toggle" aria-label="回复节奏" :aria-pressed="pacedReplyEnabled" @click="setPacedReply(!pacedReplyEnabled)">{{ pacedReplyEnabled ? '开' : '关' }}</button>
    </div>
  </section>
</template>

<style scoped>
.shell-pref { padding: 24px; margin-bottom: 28px; border: 1px solid var(--ws-border-color-3, #ebe7de); border-radius: 12px; background: var(--ws-surface-1, var(--ws-body-bg, #fffcf6)); }
.shell-pref__head { margin-bottom: 16px; }
.shell-pref__head h2 { margin: 0 0 4px; font: 600 18px var(--ws-font-display, serif); color: var(--ws-text-primary-color, #1d211f); }
.shell-pref__head p { margin: 0; font-size: 13px; color: var(--ws-text-secondary-color, #686b66); }
.shell-pref__row { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 12px 0; border-top: 1px solid var(--ws-border-color-3, #ebe7de); }
.shell-pref__copy { display: grid; gap: 3px; min-width: 0; }
.shell-pref__copy strong { font-size: 14px; color: var(--ws-text-primary-color, #1d211f); }
.shell-pref__copy span { font-size: 12px; line-height: 1.6; color: var(--ws-text-secondary-color, #686b66); }
.shell-pref__choices { display: inline-flex; flex-shrink: 0; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 8px; overflow: hidden; }
.shell-pref__choices button, .shell-pref__toggle { padding: 6px 14px; border: 0; background: transparent; color: var(--ws-text-secondary-color, #686b66); font: inherit; font-size: 13px; cursor: pointer; }
.shell-pref__choices button + button { border-left: 1px solid var(--ws-border-color, #d8d3c8); }
.shell-pref__choices button[aria-pressed='true'] { background: var(--ws-primary-color, #a6452e); color: #fff; }
.shell-pref__toggle { flex-shrink: 0; min-width: 52px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 8px; }
.shell-pref__toggle[aria-pressed='true'] { border-color: var(--ws-primary-color, #a6452e); color: var(--ws-primary-color, #a6452e); }
.shell-pref button:focus-visible { outline: 2px solid var(--ws-primary-color, #a6452e); outline-offset: 2px; }
@media (max-width: 600px) { .shell-pref { padding: 18px 16px; } .shell-pref__row { flex-direction: column; align-items: flex-start; gap: 10px; } }
</style>
