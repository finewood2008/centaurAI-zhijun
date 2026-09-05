"use strict";
/**
 * renderer/setup ViewState 派生（WP M）。
 *
 * 指南约束：页面只消费 ClaimCoordinatorSnapshot 经 deriveElectronViewState 得到的
 * 状态，不得复制状态机或按原始 DOM/Native 错误文本分支。本模块是阶段 3 的纯函数
 * 骨架——不 import electron，不持有任何秘密，可在 node 环境直接单测。
 *
 * 阶段 3 Mock 流程阶段（与指南 Mock 状态对应）：预检 → 候选选择 → 身份验证 →
 * Wi-Fi → App Proof → Owner/ACK → 完成；全程支持取消与恢复。
 */
const SETUP_STAGES = Object.freeze({
  idle: "idle",
  precheck: "precheck",
  candidate_selection: "candidate_selection",
  authentication: "authentication",
  wifi: "wifi",
  app_proof: "app_proof",
  owner_ack: "owner_ack",
  done: "done",
  cancelled: "cancelled",
  failed: "failed",
});

const STAGE_ORDER = Object.freeze([
  SETUP_STAGES.precheck,
  SETUP_STAGES.candidate_selection,
  SETUP_STAGES.authentication,
  SETUP_STAGES.wifi,
  SETUP_STAGES.app_proof,
  SETUP_STAGES.owner_ack,
  SETUP_STAGES.done,
]);

/** 从 ClaimCoordinatorSnapshot 派生出 renderer 可见的只读状态（骨架版）。 */
function deriveElectronViewState(snapshot) {
  const base = {
    stage: SETUP_STAGES.idle,
    canStart: false,
    canSelectCandidate: false,
    canCancel: false,
    candidateCount: 0,
    selectedCandidate: null,
    error: null,
    inProgress: false,
  };
  if (!snapshot || typeof snapshot !== "object") return base;
  const stage = typeof snapshot.stage === "string" && snapshot.stage in SETUP_STAGES ? snapshot.stage : SETUP_STAGES.idle;
  const candidates = Array.isArray(snapshot.candidates) ? snapshot.candidates : [];
  const selected = snapshot.selectedCandidate || null;
  const started = stage !== SETUP_STAGES.idle && stage !== SETUP_STAGES.cancelled && stage !== SETUP_STAGES.done;
  return {
    stage,
    canStart: stage === SETUP_STAGES.idle,
    canSelectCandidate: stage === SETUP_STAGES.candidate_selection && candidates.length > 0,
    canCancel: started,
    candidateCount: candidates.length,
    selectedCandidate: selected,
    error: snapshot.error || null,
    inProgress: started,
  };
}

/** 阶段间可恢复性：仅失败/取消可回到前置阶段重试；完成态不可回退。 */
function canResume(stage) {
  return stage === SETUP_STAGES.failed || stage === SETUP_STAGES.cancelled;
}

// UMD 尾部：Node 环境走 module.exports；浏览器（renderer/setup）挂到 window。
// 用 IIFE 隔离顶层声明，避免多个 classic script 共享全局词法环境时命名冲突。
(() => {
  const api = { SETUP_STAGES, STAGE_ORDER, deriveElectronViewState, canResume };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.setupViewState = api;
})();
