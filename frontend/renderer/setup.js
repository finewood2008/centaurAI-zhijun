"use strict";
/**
 * renderer/setup：本地添加 Mock 流程 UI（WP M/L）。
 *
 * 页面只消费 __SETUP_ACCESS__ 桥取回的 ClaimCoordinatorSnapshot，经
 * buildSetupUiModel 派生成展示模型渲染；本文件不复制状态机、不持有任何秘密。
 * 桥缺失（生产 gate 关闭、窗口被直接打开等）时展示不可用状态。
 */
(() => {
  const access = window.__SETUP_ACCESS__;
  const els = {
    badge: document.getElementById("setup-badge"),
    status: document.getElementById("setup-status"),
    progress: document.getElementById("setup-progress"),
    candidates: document.getElementById("setup-candidates"),
    start: document.getElementById("setup-start"),
    action: document.getElementById("setup-action"),
    resume: document.getElementById("setup-resume"),
    cancel: document.getElementById("setup-cancel"),
    wifi: document.getElementById("setup-wifi"),
    wifiSsid: document.getElementById("setup-wifi-ssid"),
    wifiPassword: document.getElementById("setup-wifi-password"),
    wifiConfirm: document.getElementById("setup-wifi-confirm"),
    error: document.getElementById("setup-error"),
    success: document.getElementById("setup-success"),
  };

  const MOCK_DEVICE_INFO = {
    serviceUuid: "5d5e1a02-0002-4000-8000-000000000000",
    fw: "mock-1.0",
  };

  // 阶段动作：只有用户手势才推进状态机（Mock 流程各阶段对应一个明确动作）。
  const STAGE_ACTIONS = {
    authentication: { label: "继续认证", handler: () => access.authenticate() },
    app_proof: { label: "继续双向校验", handler: () => access.appProof() },
    owner_ack: { label: "确认完成", handler: () => access.acknowledge() },
  };

  function render(snapshot) {
    if (typeof window.buildSetupUiModel !== "function") return;
    const model = window.buildSetupUiModel(snapshot);
    els.badge.textContent = model.badge;
    els.status.textContent = model.statusText;
    els.progress.textContent = model.progress;
    els.error.style.display = model.errorText || model.cancelledText ? "block" : "none";
    els.error.textContent = model.errorText || model.cancelledText || "";
    els.success.style.display = model.stage === "done" ? "block" : "none";
    els.success.textContent = "设备已添加，可关闭本窗口。";
    els.start.hidden = !model.canStart;
    els.start.disabled = !model.canStart;
    els.resume.hidden = !model.canResume;
    els.resume.disabled = !model.canResume;
    els.cancel.hidden = !model.canCancel;
    els.cancel.disabled = !model.canCancel;
    const stageAction = STAGE_ACTIONS[model.stage];
    els.action.hidden = !stageAction;
    els.action.textContent = stageAction ? stageAction.label : "继续";
    els.action.onclick = stageAction ? stageAction.handler : null;
    els.wifi.hidden = model.stage !== "wifi";
    els.candidates.innerHTML = "";
    for (const candidate of model.candidates) {
      const li = document.createElement("li");
      li.className = candidate.selected ? "selected" : "";
      const name = document.createElement("span");
      name.textContent = candidate.name;
      li.appendChild(name);
      if (candidate.rssi !== null) {
        const rssi = document.createElement("span");
        rssi.className = "rssi";
        rssi.textContent = `${candidate.rssi} dBm`;
        li.appendChild(rssi);
      }
      li.addEventListener("click", () => {
        if (access && model.canSelectCandidate) access.selectCandidate(candidate.id);
      });
      els.candidates.appendChild(li);
    }
  }

  function setup() {
    if (!access) {
      els.badge.textContent = "不可用";
      els.status.textContent = "本地添加功能未开放。";
      return;
    }
    els.start.addEventListener("click", () => access.start(MOCK_DEVICE_INFO));
    els.resume.addEventListener("click", () => access.resume());
    els.cancel.addEventListener("click", () => access.cancel("user_cancelled"));
    els.wifiConfirm.addEventListener("click", () => {
      access.provisionWifi(els.wifiSsid.value.trim(), els.wifiPassword.value);
    });
    access.onStateChanged(render);
    access.getState().then(render).catch((err) => {
      els.status.textContent = `读取状态失败：${err && err.message}`;
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
