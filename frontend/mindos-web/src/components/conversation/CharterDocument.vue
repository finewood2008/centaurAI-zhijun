<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { charterExecutionLabel, charterKinds, charterSourceLabel } from '@/shared/charterWorkspace'
import type { CharterClause } from '@/services/api'
const props = defineProps<{ document: string; clauses?: CharterClause[]; appearance?: 'compact' | 'codex' }>()
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true })
// Reading a private document must not contact embedded remote image URLs.
markdown.disable('image')
const rendered = computed(() => markdown.render(props.document))
</script>
<template>
  <div class="charter-document" :class="{ 'charter-document--codex': appearance === 'codex' }">
    <div class="charter-document__text" v-html="rendered" />
    <details v-if="clauses?.length"><summary>查看条款类型、作用与来源</summary>
      <article v-for="clause in clauses" :key="clause.id">
        <p>{{ clause.text }}</p>
        <small>{{ charterKinds[clause.kind] }} · {{ charterExecutionLabel(clause) }}<template v-if="clause.context"> · {{ clause.context }}</template></small>
        <p v-if="clause.quote" class="charter-document__quote">原话：“{{ clause.quote }}”</p>
        <p v-if="clause.clarification">待澄清：{{ clause.clarification }}</p>
        <small v-for="source in clause.sources" :key="`${source.kind}:${source.id}:${source.version}`">来源 {{ charterSourceLabel(source.kind) }} · {{ source.id }} · 版本 {{ source.version }}<br /></small>
      </article>
    </details>
  </div>
</template>
<style scoped>
.charter-document { min-width:0; font-size:14px; line-height:1.85; overflow-wrap:anywhere; }.charter-document__text :deep(h2) { margin:1.5em 0 .5em; font-size:20px; font-family:var(--ws-font-display); }.charter-document__text :deep(h3) { font-size:17px; }.charter-document__text :deep(p) { margin:.6em 0; }.charter-document details { margin-top:20px; border-top:1px solid var(--ws-border-color); padding-top:12px; }.charter-document summary { cursor:pointer; }.charter-document article { padding:10px 0; border-bottom:1px solid var(--ws-border-color-3); }.charter-document small,.charter-document__quote { color:var(--ws-text-secondary-color); font-size:12px; }
.charter-document__text :deep(h1) { font:600 26px var(--ws-font-display); line-height:1.4; margin:.4em 0 1em; }
.charter-document__text :deep(pre) { white-space:pre-wrap; overflow-wrap:anywhere; padding:12px; background:var(--ws-surface-2); border-radius:8px; }
.charter-document__text :deep(code) { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.88em; }
.charter-document__text :deep(table) { display:block; max-width:100%; overflow-x:auto; border-collapse:collapse; }
.charter-document__text :deep(th),.charter-document__text :deep(td) { border:1px solid var(--ws-border-color); padding:6px 10px; }
.charter-document__text :deep(blockquote) { margin:1em 0; padding-left:16px; border-left:2px solid var(--ws-border-color); color:var(--ws-text-secondary-color); }
.charter-document__text :deep(ul),.charter-document__text :deep(ol) { padding-left:1.5em; margin:.6em 0; }
.charter-document__text :deep(a) { text-decoration:underline; text-decoration-thickness:1px; text-underline-offset:3px; }
.charter-document--codex { max-width:720px; margin:0 auto; color:#292b28; font:16px/2 var(--ws-font-display); letter-spacing:.015em; }
.charter-document--codex .charter-document__text :deep(> :first-child) { margin-top:0; }
.charter-document--codex .charter-document__text :deep(h1) { position:relative; margin:0 0 1.65em; padding:0 0 .85em; color:#202320; font:600 clamp(28px,4vw,38px)/1.4 var(--ws-font-display); letter-spacing:.12em; text-align:center; }
.charter-document--codex .charter-document__text :deep(h1)::after { content:""; position:absolute; bottom:0; left:50%; width:54px; height:2px; background:var(--ws-primary-color); transform:translateX(-50%); }
.charter-document--codex .charter-document__text :deep(h2) { margin:2.2em 0 .8em; padding:0 0 .48em; border-bottom:1px solid #dcd3c6; color:#252825; font:600 22px/1.55 var(--ws-font-display); letter-spacing:.08em; }
.charter-document--codex .charter-document__text :deep(h2)::before { content:""; display:inline-block; width:4px; height:17px; margin-right:10px; background:var(--ws-primary-color); vertical-align:-2px; }
.charter-document--codex .charter-document__text :deep(h3) { margin:1.7em 0 .6em; color:#30332f; font:600 18px/1.6 var(--ws-font-display); letter-spacing:.05em; }
.charter-document--codex .charter-document__text :deep(p) { margin:.9em 0; }
.charter-document--codex .charter-document__text :deep(strong) { color:#202320; font-weight:700; }
.charter-document--codex .charter-document__text :deep(hr) { height:1px; margin:2.5em auto; border:0; background:linear-gradient(90deg,transparent,#cfc5b7,transparent); }
.charter-document--codex .charter-document__text :deep(blockquote) { margin:1.5em 0; padding:8px 0 8px 22px; border-left:3px solid rgba(166,69,46,.55); color:#555a54; font-size:.96em; }
.charter-document--codex .charter-document__text :deep(ul),.charter-document--codex .charter-document__text :deep(ol) { margin:.9em 0; padding-left:1.65em; }
.charter-document--codex .charter-document__text :deep(li) { margin:.35em 0; padding-left:.2em; }.charter-document--codex .charter-document__text :deep(li)::marker { color:var(--ws-primary-color); }
.charter-document--codex .charter-document__text :deep(pre) { max-width:100%; overflow:auto; padding:16px; border:1px solid #e1d9cd; border-radius:2px; background:#f8f3ea; font:13px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace; }
.charter-document--codex .charter-document__text :deep(table) { width:100%; margin:1.5em 0; font-family:inherit; font-size:14px; line-height:1.65; }
.charter-document--codex .charter-document__text :deep(th) { background:#f4eee4; font-weight:600; }.charter-document--codex .charter-document__text :deep(th),.charter-document--codex .charter-document__text :deep(td) { border-color:#d8cfc1; padding:9px 12px; }
.charter-document--codex details { margin-top:34px; padding-top:16px; border-top:1px solid #d9d0c3; font-family:"PingFang SC","Noto Sans SC",sans-serif; font-size:13px; line-height:1.75; }
@media(max-width:480px) { .charter-document--codex { font-size:16px; line-height:1.9; }.charter-document--codex .charter-document__text :deep(h1) { font-size:28px; }.charter-document--codex .charter-document__text :deep(h2) { font-size:20px; } }
</style>
