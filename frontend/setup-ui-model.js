"use strict";
/**
 * Setup 窗口 UI 模型（WP M，renderer/setup 消费侧）。
 *
 * 指南约束：页面只消费 ClaimCoordinatorSnapshot 经 deriveElectronViewState 得到的
 * 状态，不得复制状态机或按原始 DOM/Native 错误文本分支。本模块把 snapshot 派生成
 * 纯展示模型（阶段徽章/按钮可用性/候选列表/错误文案），DOM 层只做渲染。
 * 纯函数，不 import electron，可在 node 环境直接单测。
 *
 * 整体包在 IIFE 内：本文件同时供 Node（require）与浏览器 classic script 加载，
 * 顶层声明隔离，避免与 setup-view-state.js 等共享全局词法环境时命名冲突。
 */
(() => {
  const setupViewState =
    typeof module !== "undefined" && module.exports ? require("./setup-view-state.js") : window.setupViewState;
  const { SETUP_STAGES, deriveElectronViewState } = setupViewState;

  const STAGE_LABELS = Object.freeze({
    [SETUP_STAGES.idle]: "待开始",
    [SETUP_STAGES.precheck]: "预检中",
    [SETUP_STAGES.candidate_selection]: "选择设备",
    [SETUP_STAGES.authentication]: "身份验证",
    [SETUP_STAGES.wifi]: "配置 Wi-Fi",
    [SETUP_STAGES.app_proof]: "双向校验",
    [SETUP_STAGES.owner_ack]: "等待确认",
    [SETUP_STAGES.done]: "已完成",
    [SETUP_STAGES.cancelled]: "已取消",
    [SETUP_STAGES.failed]: "失败",
  });

  const ERROR_LABELS = Object.freeze({
    bluetooth_unavailable: "蓝牙未开启或不可用，请开启蓝牙后重试。",
    device_info_missing: "未获取到设备信息，请重试。",
    permission_denied: "未获得设备访问权限（可能已取消选择）。",
    candidate_not_found: "所选设备已不可用，请重新选择。",
    gatt_disconnected: "连接已断开（GATT 断线），可重试恢复。",
    wifi_credentials_missing: "缺少 Wi-Fi 名称或密码。",
    ack_timeout: "盒子未确认（ACK 超时），可重试。",
  });

  function stageProgress(stage) {
    const order = [SETUP_STAGES.precheck, SETUP_STAGES.candidate_selection, SETUP_STAGES.authentication, SETUP_STAGES.wifi, SETUP_STAGES.app_proof, SETUP_STAGES.owner_ack, SETUP_STAGES.done];
    const index = order.indexOf(stage);
    return index >= 0 ? { current: index + 1, total: order.length } : null;
  }

  /** snapshot → 纯展示模型。不抛出；任何异常输入回退到 idle。 */
  function buildSetupUiModel(snapshot) {
    const view = deriveElectronViewState(snapshot);
    const stage = view.stage;
    const errorText = view.error ? ERROR_LABELS[view.error] || view.error : null;
    const cancelledText = snapshot && snapshot.cancelledReason ? `原因：${snapshot.cancelledReason}` : null;
    const progress = stageProgress(stage);
    const candidates = Array.isArray(snapshot && snapshot.candidates)
      ? snapshot.candidates.map((candidate) => ({
          id: candidate.id,
          name: candidate.name || candidate.id,
          rssi: typeof candidate.rssi === "number" ? candidate.rssi : null,
          selected: Boolean(view.selectedCandidate && view.selectedCandidate.id === candidate.id),
        }))
      : [];
    return {
      stage,
      badge: STAGE_LABELS[stage] || stage,
      statusText: statusTextFor(stage, errorText),
      progress: progress ? `步骤 ${progress.current}/${progress.total}` : "",
      canStart: view.canStart,
      canSelectCandidate: view.canSelectCandidate,
      canCancel: view.canCancel,
      canResume: canResumeFrom(stage),
      candidates,
      selectedId: view.selectedCandidate ? view.selectedCandidate.id : null,
      errorText,
      cancelledText,
      wifiConfigured: Boolean(snapshot && snapshot.wifiConfigured),
    };
  }

  function statusTextFor(stage, errorText) {
    if (stage === SETUP_STAGES.done) return "设备已添加，可关闭本窗口。";
    if (stage === SETUP_STAGES.cancelled) return "已取消本地添加流程。";
    if (stage === SETUP_STAGES.failed) return errorText || "流程失败，可重试。";
    if (stage === SETUP_STAGES.idle) return "请选择要添加的 AI 盒子。";
    return "正在执行…";
  }

  function canResumeFrom(stage) {
    return stage === SETUP_STAGES.failed || stage === SETUP_STAGES.cancelled;
  }

  const api = { buildSetupUiModel, STAGE_LABELS, ERROR_LABELS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.buildSetupUiModel = buildSetupUiModel;
})();
