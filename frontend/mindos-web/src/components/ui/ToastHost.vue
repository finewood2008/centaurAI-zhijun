<script setup lang="ts">
// Toast 宿主：渲染应用级 toastStore 中的提示（底部居中，2.5 秒自动关闭，错误可手动关闭）。
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-vue-next'
import { closeToast, useToastList } from '@/composables/toastStore'

const toasts = useToastList()

const ICONS = {
  success: CheckCircle2,
  error: AlertCircle,
  info: Info,
}
</script>

<template>
  <slot />
  <Teleport to="body">
    <div class="ws-toast-host" aria-live="polite" aria-atomic="true">
      <TransitionGroup name="ws-toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          class="ws-toast"
          :class="[`ws-toast--${t.type}`, { 'is-closing': t.closing }]"
          role="status"
        >
          <component :is="ICONS[t.type]" class="ws-toast__icon" :size="18" aria-hidden="true" />
          <span class="ws-toast__message">{{ t.message }}</span>
          <button
            v-if="t.type === 'error'"
            type="button"
            class="ws-toast__close"
            aria-label="关闭提示"
            @click="closeToast(t.id)"
          >
            <X :size="14" aria-hidden="true" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.ws-toast-host {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  z-index: 3000;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  pointer-events: none;
}

.ws-toast {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: min(420px, 90vw);
  padding: 9px 14px;
  border-radius: var(--ws-radius, 6px);
  background: var(--ws-text-primary-color, #303133);
  color: var(--ws-white, #fff);
  font-size: 13px;
  box-shadow: var(--ws-shadow-md, 0 6px 20px rgba(0, 0, 0, 0.1));
  pointer-events: auto;
}

.ws-toast__icon {
  flex-shrink: 0;
}

.ws-toast--success .ws-toast__icon {
  color: var(--ws-success-color, #12cd3d);
}
.ws-toast--error .ws-toast__icon {
  color: var(--ws-danger-color, #ff4918);
}
.ws-toast--info .ws-toast__icon {
  color: var(--ws-main-color, #1b99ff);
}

.ws-toast__message {
  line-height: 1.5;
  word-break: break-word;
}

.ws-toast__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  border: none;
  background: transparent;
  color: var(--ws-white, #fff);
  opacity: 0.7;
  cursor: pointer;
  border-radius: var(--ws-radius-sm, 4px);
}
.ws-toast__close:hover {
  opacity: 1;
}

.ws-toast-enter-active,
.ws-toast-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.ws-toast-enter-from,
.ws-toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
