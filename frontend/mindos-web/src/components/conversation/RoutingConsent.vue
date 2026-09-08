<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { needsDeConsent, routeQuestion } from '@/services/taskRouting'
const dialog = ref<HTMLDialogElement | null>(null)
const keys = ref<string[]>([])
const question = computed(() => routeQuestion.value)
const deConsent = computed(() => !!question.value && needsDeConsent(question.value.preview))
const grantable = computed(() => question.value?.preview.sources.filter(s => !s.blocked && (deConsent.value || question.value!.preview.missing.includes(s.key))) || [])
const unavailable = computed(() => question.value?.preview.sources.filter(s => s.blocked) || [])
watch(question, async value => {
  keys.value = value?.preview.sources.filter(s => !s.blocked && value.preview.missing.includes(s.key)).map(s => s.key) || []
  await nextTick()
  if (value && !dialog.value?.open) dialog.value?.showModal()
  else if (!value) dialog.value?.close()
})
</script>
<template>
  <dialog ref="dialog" class="route-consent" aria-labelledby="route-title" @cancel.prevent="question?.done({ action: 'cancel' })">
    <template v-if="question?.preview.charterConflict">
      <h2 id="route-title">这次处理与你的章程约定不同</h2>
      <p>{{ question.preview.charterConflict.detail }}</p>
      <p><RouterLink :to="{ path: '/me/charter', query: { version: question.preview.charterConflict.charterVersion } }">人生章程第 {{ question.preview.charterConflict.charterVersion }} 版</RouterLink></p>
      <blockquote v-for="clause in question.preview.charterConflict.clauses" :key="clause.id">{{ clause.text }}</blockquote>
      <p>本次原计划交给 {{ question.preview.service.name }} · {{ question.preview.purposeLabel }}。</p>
      <p class="route-tip">只为本轮设置例外，不改动正式章程，也不代表同意发送资料。需要的资料授权会另外核对。</p>
      <footer>
        <button @click="question.done({ action: 'local' })">遵守章程，仅本地处理</button>
        <button v-if="question.preview.charterConflict.canOverride" @click="question.done({ action: 'exception' })">仅本轮例外，继续核对资料权限</button>
        <button @click="question.done({ action: 'cancel' })">取消，保留输入</button>
      </footer>
    </template>
    <template v-else-if="question">
      <h2 id="route-title">{{ deConsent ? '是否将本次内容交给在线模型？' : grantable.length ? '这次要让在线模型使用哪些内容？' : '选用的内容暂时不能交给在线模型' }}</h2>
      <p>{{ question.preview.service.name }} · {{ question.preview.service.model }} · {{ question.preview.purposeLabel }}</p>
      <p>{{ deConsent ? '原文件留在盒子。' : '原文件留在本机。' }}下方显示实际拟发送的文字；已发送的内容无法通过撤销授权收回。</p>
      <p v-if="deConsent" class="route-tip">本次确认仅适用于当前任务、输入、来源版本和接收服务。资料来源默认授权不能代替本次在线发送确认；改动内容后需要重新核对。当前桌面版本没有关闭此确认的总开关。</p>
      <p v-else class="route-tip">相同服务、版本和用途已批准的内容不再询问。在「模型与授权」中可分别设置默认授权，或记住资料受限时的处理方式。</p>
      <p v-if="question.preview.charterBasis?.version" class="route-tip">本轮参考人生章程第 {{ question.preview.charterBasis.version }} 版 · {{ question.preview.charterBasis.clauseIds.length }} 条约定。确认章程不等于允许外发。</p>
      <details v-if="question.preview.charterUnresolved?.length"><summary>有 {{ question.preview.charterUnresolved.length }} 条约定尚需澄清执行方式</summary><p v-for="clause in question.preview.charterUnresolved" :key="clause.id">{{ clause.text }}：{{ clause.reason }}</p></details>
      <p v-if="deConsent">本次范围：{{ question.preview.request.messages.length }} 条消息、系统提示，以及 {{ question.preview.sources.length }} 项来源。{{ question.preview.sources.length ? '所列来源将一起用于本次任务。' : '此次未引用资料，仍会发送下方的输入与系统提示。' }}</p>
      <fieldset v-if="grantable.length"><legend>{{ deConsent ? '本次将发送的全部来源' : '本次可以授权的来源' }}</legend>
        <label v-for="source in grantable" :key="source.key + source.version" class="route-source">
          <input v-if="!deConsent" v-model="keys" type="checkbox" :value="source.key" :disabled="!!source.blocked || !question.preview.missing.includes(source.key)" />
          <span>{{ source.title }} <small>版本 {{ source.version.slice(0, 8) }}</small>
            <strong v-if="source.blocked">{{ source.blocked }}</strong>
            <small v-else-if="!deConsent && !question.preview.missing.includes(source.key)">本服务、版本及用途已获授权</small>
          </span>
        </label>
      </fieldset>
      <details v-if="unavailable.length"><summary>{{ unavailable.length }} 项选用内容暂不可用，不能通过重复授权解决</summary>
        <p v-for="source in unavailable" :key="source.key + source.version">{{ source.title }}：{{ source.blocked }}</p>
        <p>可选择本地处理，或移除相关资料。已删除或版本无法核实的内容不会恢复使用。</p>
      </details>
      <details><summary>查看实际拟发送的文字与完整来源</summary>
        <p v-for="source in question.preview.sources" :key="source.key + source.version">{{ source.title }} · 版本 {{ source.version.slice(0, 8) }}</p>
        <pre v-for="(message, i) in question.preview.request.messages" :key="i">{{ message.role }}：{{ message.content }}</pre>
        <details><summary>完整系统提示与选取的证据</summary><pre>{{ question.preview.request.system }}</pre></details>
      </details>
      <p v-if="question.preview.excluded.length">有 {{ question.preview.excluded.length }} 条历史或资料未纳入本轮；知君需要时会请你补充，不会猜测。</p>
      <footer>
        <button v-if="deConsent" :disabled="unavailable.length > 0" @click="question.done({ action: 'allow', keys: question.preview.sources.map(source => source.key) })">允许本次内容发送到 {{ question.preview.service.name }}</button>
        <button v-else-if="grantable.length" :disabled="!keys.length" @click="question.done({ action: 'allow', keys: [...keys] })">允许所选内容用于该服务和用途</button>
        <button @click="question.done({ action: 'local' })">仅本地处理</button>
        <button v-if="question.allowOmit" @click="question.done({ action: 'omit' })">不使用这些资料继续</button>
        <button @click="question.done({ action: 'cancel' })">取消</button>
      </footer>
    </template>
  </dialog>
</template>
<style scoped>
.route-consent{margin:auto;width:min(700px,calc(100vw - 32px));max-height:85vh;overflow:auto;background:var(--ws-bg-color,#fffaf4);color:var(--ws-text-color,#282820);border:1px solid #cec8bc;border-radius:16px;padding:24px;box-sizing:border-box}
.route-consent::backdrop{background:#0006}.route-consent h2{font-size:20px}.route-consent p{font-size:14px;line-height:1.6}
.route-consent .route-tip{font-size:12px;color:var(--ws-text-secondary-color,#686b66)}
.route-source{display:flex;gap:9px;padding:9px 0}.route-source small,.route-source strong{display:block;font-size:12px}.route-source strong{color:#a9402b}
fieldset{border:1px solid #ddd4c7;margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 inherit;background:#8881;padding:10px;border-radius:6px}footer{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}button{padding:9px 12px;border:1px solid #ccc1b3;background:transparent;color:inherit;border-radius:8px;cursor:pointer}button:disabled{opacity:.4}summary{cursor:pointer;margin:8px 0}
</style>
