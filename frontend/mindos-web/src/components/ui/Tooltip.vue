<script setup lang="ts">
// 轻量 CSS Tooltip：悬停/聚焦显示简短提示文字，无额外依赖。
withDefaults(
  defineProps<{
    content?: string
    placement?: 'top' | 'bottom' | 'left' | 'right'
  }>(),
  {
    content: '',
    placement: 'top',
  },
)
</script>

<template>
  <span
    class="ws-tooltip"
    :class="`ws-tooltip--${placement}`"
  >
    <slot />
    <span
      v-if="content"
      class="ws-tooltip__tip"
      role="tooltip"
    >
      {{ content }}
    </span>
  </span>
</template>

<style scoped>
.ws-tooltip {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  outline: none;
}

.ws-tooltip__tip {
  position: absolute;
  z-index: 2000;
  padding: 4px 8px;
  border-radius: var(--ws-radius-sm, 4px);
  background: var(--ws-text-primary-color, #1d211f);
  color: var(--ws-white, #fff);
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  transform: translateY(2px);
  transition:
    opacity 0.15s ease,
    transform 0.15s ease;
}

.ws-tooltip--top .ws-tooltip__tip {
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translate(-50%, 2px);
}
.ws-tooltip--top:hover .ws-tooltip__tip,
.ws-tooltip--top:focus-within .ws-tooltip__tip {
  opacity: 1;
  transform: translate(-50%, 0);
}

.ws-tooltip--bottom .ws-tooltip__tip {
  top: calc(100% + 6px);
  left: 50%;
  transform: translate(-50%, -2px);
}
.ws-tooltip--bottom:hover .ws-tooltip__tip,
.ws-tooltip--bottom:focus-within .ws-tooltip__tip {
  opacity: 1;
  transform: translate(-50%, 0);
}

.ws-tooltip--left .ws-tooltip__tip {
  right: calc(100% + 6px);
  top: 50%;
  transform: translate(2px, -50%);
}
.ws-tooltip--left:hover .ws-tooltip__tip,
.ws-tooltip--left:focus-within .ws-tooltip__tip {
  opacity: 1;
  transform: translate(0, -50%);
}

.ws-tooltip--right .ws-tooltip__tip {
  left: calc(100% + 6px);
  top: 50%;
  transform: translate(-2px, -50%);
}
.ws-tooltip--right:hover .ws-tooltip__tip,
.ws-tooltip--right:focus-within .ws-tooltip__tip {
  opacity: 1;
  transform: translate(0, -50%);
}
</style>
