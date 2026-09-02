<script setup lang="ts">
// 一张小图解释「本体」：中心是我，三个环——实线（我确认过的）、点线（很久没再提）、朱砂虚线（信任边界，外面是知君的推测）。
// 与本体全景同一套视觉词汇；含义同时由文字给出。
defineProps<{ compact?: boolean }>()
</script>

<template>
  <figure class="zj-explainer" :class="{ 'is-compact': compact }">
    <svg viewBox="0 0 360 200" role="img" aria-label="本体图解：中心是我；离中心越近，越是我确认过的；朱砂虚线外是知君的推测，等我确认" class="zj-explainer__svg">
      <g transform="translate(120 100)">
        <circle r="92" fill="#FBF8F1" />
        <circle r="92" fill="none" stroke="#A6452E" stroke-width="1.5" stroke-dasharray="7 5" />
        <circle r="66" fill="#F7F3EA" />
        <circle r="66" fill="none" stroke="#8B8E88" stroke-width="1.2" stroke-dasharray="1.5 5" stroke-linecap="round" />
        <circle r="40" fill="#FFFCF6" />
        <circle r="40" fill="none" stroke="#1D211F" stroke-width="1.2" />
        <circle r="17" fill="#FFFCF6" stroke="#A6452E" stroke-width="2" />
        <text y="6" text-anchor="middle" class="zj-explainer__seal">我</text>
        <!-- 示例点 -->
        <circle cx="26" cy="-20" r="4" fill="#1D211F" />
        <circle cx="-30" cy="18" r="4" fill="#4A7C59" />
        <circle cx="12" cy="52" r="4" fill="#A6452E" />
        <circle cx="70" cy="-62" r="4" fill="#FFFCF6" stroke="#A6452E" stroke-width="1.5" stroke-dasharray="2 2" />
        <circle cx="-85" cy="-40" r="4" fill="#FFFCF6" stroke="#A6452E" stroke-width="1.5" stroke-dasharray="2 2" />
        <!-- 引线 -->
        <line x1="40" y1="0" x2="126" y2="0" stroke="#D8D3C8" stroke-width="1" />
        <line x1="47" y1="-47" x2="126" y2="-52" stroke="#D8D3C8" stroke-width="1" />
        <line x1="65" y1="65" x2="126" y2="62" stroke="#D8D3C8" stroke-width="1" />
      </g>
      <g class="zj-explainer__captions">
        <text x="250" y="52" class="zj-explainer__cap">实线环内</text>
        <text x="250" y="67" class="zj-explainer__sub">我确认过的，知君当作事实</text>
        <text x="250" y="104" class="zj-explainer__cap">点线环</text>
        <text x="250" y="119" class="zj-explainer__sub">很久没再提，可能变了</text>
        <text x="250" y="166" class="zj-explainer__cap zj-explainer__cap--cinnabar">朱砂虚线外</text>
        <text x="250" y="181" class="zj-explainer__sub">知君的推测，等我确认</text>
      </g>
    </svg>
    <figcaption class="zj-explainer__caption">这是属于我的本体：离中心越近，越是我确认过的；知君的推测，只有我点头才会留下。</figcaption>
  </figure>
</template>

<style scoped>
.zj-explainer {
  margin: 0;
  max-width: 420px;
}
.zj-explainer.is-compact {
  max-width: 360px;
}
.zj-explainer__svg {
  display: block;
  width: 100%;
  height: auto;
  font-family: var(--ws-font-display, serif);
}
.zj-explainer__seal {
  font-size: 15px;
  fill: #a6452e;
}
.zj-explainer__cap {
  font-size: 13px;
  fill: #1d211f;
}
.zj-explainer__cap--cinnabar {
  fill: #a6452e;
}
.zj-explainer__sub {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  font-size: 10.5px;
  fill: #686b66;
}
.zj-explainer__caption {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
</style>
