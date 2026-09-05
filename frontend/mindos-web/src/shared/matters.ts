import type { ArtifactKind } from '../services/matters'

// These are requests to the assistant, never pre-filled beliefs or facts about the user.
export const artifactPrompts: Record<Exclude<ArtifactKind, 'freeform'>, string> = {
  communication: '请结合这件事已经明确的目标、人物和约束，帮我准备一份重要沟通提纲。包括想达成什么、如何开场、需要谈清的分歧和可执行的下一步。把未知信息标为待补充，不替我编造对方的想法。',
  decision_memo: '请把这件事整理成一份决策备忘录：已知事实与约束、可选方案、主要取舍、你的建议及其前提、下一步。你新提出的选项请单独标明；暂未决定的地方留待我确认。',
  meeting_prep: '请结合这件事已有的背景，帮我准备一份会前材料：会议目标、议题顺序、需要核对的信息和希望形成的决定。没有说过的时间、人员或数据请留空，不要编造。',
  action_summary: '请根据这件事已经聊到的进展，整理一份行动小结：已明确的决定、下一步、已说清的负责人或时间，以及还未解决的问题。没有确定的安排请标为待确认。',
}
export function matterDraft(matter: { title: string; goal: string; context: string; nextStep: string; outcome: string; status: string }) {
  return { title: matter.title, goal: matter.goal, context: matter.context, nextStep: matter.nextStep, outcome: matter.outcome, status: matter.status }
}
export function cleanFilename(title: string): string {
  return (title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-').trim().slice(0, 80) || '知君文稿') + '.md'
}
