<script setup lang="ts">
// 「带一件事来」：三张起手卡（文案沿用对话页），点一下带着话头去对话页（/chat?say=…），由对话页放进输入框。
// 上方若有没聊完的建档会话，先给一张「继续建档 · 还差 N 问」卡。
interface Starter { title: string; desc: string; text: string; deliberate?: boolean }
const STARTERS: Starter[] = [
  { title: '我在考虑一件事', desc: '把选项、倾向和把握说清楚，聊完会落成判断簿里一条可回访的记录', text: '我在考虑一件事：', deliberate: true },
  { title: '最近发生了……', desc: '说说这周让你在意的事，知君会把它和你以前说过的连起来', text: '最近发生了一件事，' },
  { title: '你怎么看我？', desc: '让知君用它目前对你的认识说说看，不对的地方你直接改', text: '基于你目前对我的认识，说说你眼中的我，哪些地方你其实不确定？' },
]

defineProps<{ pendingOnboarding?: { id: string; remaining: number } | null }>()
defineEmits<{ (e: 'pick', text: string, deliberate: boolean): void; (e: 'resume', id: string): void }>()
</script>

<template>
  <section class="zj-today-section" data-testid="today-bring" aria-label="带一件事来">
    <button v-if="pendingOnboarding" type="button" class="zj-bring__resume" data-testid="today-resume-onboarding" @click="$emit('resume', pendingOnboarding.id)">
      <span class="zj-seal zj-seal--accent">建档</span>
      <span class="zj-bring__resume-title">继续建档 · 还差 {{ pendingOnboarding.remaining }} 问</span>
      <span class="zj-bring__resume-desc">上次认识你的对话还没聊完，接着聊，本体图会继续亮起来。</span>
    </button>
    <h2 class="zj-today-section__title">带一件事来</h2>
    <div class="zj-bring__cards" role="group" aria-label="起个头">
      <button v-for="s in STARTERS" :key="s.title" type="button" class="zj-bring__card" @click="$emit('pick', s.text, !!s.deliberate)">
        <span class="zj-bring__card-title">{{ s.title }}</span>
        <span class="zj-bring__card-desc">{{ s.desc }}</span>
      </button>
    </div>
  </section>
</template>

<style scoped>
.zj-bring__resume {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  width: 100%;
  margin: 0 0 20px;
  padding: 14px;
  border: 1px dashed var(--ws-primary-color, #a6452e);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}
.zj-bring__resume:hover {
  border-style: solid;
}
.zj-bring__resume-title {
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-bring__resume-desc {
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
.zj-bring__cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.zj-bring__card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
  border: 1px solid var(--ws-border-color-3, #ebe7de);
  border-radius: var(--ws-radius-lg, 8px);
  background: var(--ws-card-bg, #fff);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.zj-bring__card:hover {
  border-color: var(--ws-primary-color, #a6452e);
  transform: translateY(-1px);
}
.zj-bring__card-title {
  font-family: var(--ws-font-display, serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ws-text-primary-color, #1d211f);
}
.zj-bring__card-desc {
  font-size: 13px;
  line-height: 1.6;
  color: var(--ws-text-secondary-color, #686b66);
}
@media (max-width: 767px) {
  .zj-bring__cards {
    grid-template-columns: 1fr;
  }
}
</style>
