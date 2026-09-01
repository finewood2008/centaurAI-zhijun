"use strict";
/**
 * typed IPC 通道清单（WP M）。
 *
 * 主进程/渲染进程共享的唯一通道常量来源：preload 暴露、main 注册、renderer 消费
 * 全部引用本表，杜绝魔法字符串漂移。Setup（阶段 3）通道默认不注册——main.js 在
 * 对应 Feature Gate 未开启时不注册 handler，渲染端调用即被拒（fail-closed）。
 *
 * 命名空间：
 *   rpc / mindos:*   —— 阶段 2 既有通道（BackendRpc、连通票据）
 *   setup:*          —— 阶段 3 本地添加窗口通道（骨架期仅声明，gate 开启后启用）
 */
const IPC = Object.freeze({
  rpc: "rpc",
  backendBaseUrl: "backend:base-url",
  saveMcpCa: "save-mcp-ca",
  connectivityTicket: "mindos:connectivity-ticket",
  setup: Object.freeze({
    open: "setup:open",
    close: "setup:close",
    state: "setup:state",
    start: "setup:start-provisioning",
    selectCandidate: "setup:select-candidate",
    authenticate: "setup:authenticate",
    provisionWifi: "setup:provision-wifi",
    appProof: "setup:app-proof",
    acknowledge: "setup:acknowledge",
    cancel: "setup:cancel",
    resume: "setup:resume",
    stateChanged: "setup:state-changed",
    ble: Object.freeze({
      scan: "setup:ble:scan",
      select: "setup:ble:select",
      status: "setup:ble:status",
      disconnect: "setup:ble:disconnect",
    }),
  }),
});

module.exports = { IPC };
