<script setup lang="ts">
// 知君气泡下的一行极淡状态字：「参考了你记下的 3 条」。有出处时可点，点开才展开回答依据。
import { computed } from 'vue'
import type { ProvenanceEvent } from '@/services/api'
import { statusLineText, type StatusLineStatus } from './statusLine'

const props = defineProps<{
  provenance?: Partial<ProvenanceEvent> | null
  meta?: { external?: boolean } | null
  status?: StatusLineStatus
  initiated?: boolean
  channel?: boolean
  open?: boolean
}>()
const emit = defineEmits<{ (e: 'toggle'): void }>()

const text = computed(() => statusLineText(props.provenance, props.meta, props.status, !!props.initiated, { channel: !!props.channel }))
const expandable = computed(() => !!props.provenance && props.status !== 'streaming')
</script>

<template>
  <button
    v-if="expandable && text"
    type="button"
    class="zj-status zj-status--button"
    data-testid="status-line"
    :aria-expanded="!!open"
    :title="open ? '收起回答依据' : '看回答依据'"
    @click="emit('toggle')"
  >{{ text }}</button>
  <span v-else-if="text" class="zj-status" data-testid="status-line">{{ text }}</span>
</template>

<style scoped>
.zj-status {
  display: inline-block;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ws-text-placeholder-color, #a3a69f);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  text-align: left;
}
.zj-status--button {
  cursor: pointer;
  border-bottom: 1px dotted transparent;
}
.zj-status--button:hover,
.zj-status--button[aria-expanded='true'] {
  color: var(--ws-text-secondary-color, #686b66);
  border-bottom-color: currentColor;
}
</style>
