// provisioning/preload-formal.mjs
var import_electron = require("electron");

// node_modules/@nexusaos/device-discovery-electron/dist/main.js
var DISCOVERY_PICKER_CHANNELS = Object.freeze({
  candidates: "nexusaos:discovery:candidates",
  select: "nexusaos:discovery:select",
  cancel: "nexusaos:discovery:cancel",
  settled: "nexusaos:discovery:settled"
});

// node_modules/@nexusaos/local-provisioning-contracts/dist/index.js
var PROVISIONING_SERVICE_UUID = "f6c7b7a0-5a0f-4b7f-9f3f-4d3f7b5a1000";
var DEVICE_INFO_UUID = "f6c7b7a1-5a0f-4b7f-9f3f-4d3f7b5a1000";
var COMMAND_UUID = "f6c7b7a2-5a0f-4b7f-9f3f-4d3f7b5a1000";
var STATUS_UUID = "f6c7b7a3-5a0f-4b7f-9f3f-4d3f7b5a1000";
var SETUP_NAME_PREFIX = "CentaurOS-Setup-";
var GATT_HEADER_BYTES = 8;
var GATT_PAYLOAD_BYTES = 12;
var GATT_MAX_LOGICAL_BYTES = 2048;
var GATT_MAX_FRAGMENTS = 171;
var RETRYABLE = /* @__PURE__ */ new Set([
  "LOCAL_CONNECT_TIMEOUT",
  "GATT_FRAGMENT_INCOMPLETE",
  "LOCAL_TRANSPORT_DISCONNECTED"
]);
var LocalProvisioningFailure = class extends Error {
  code;
  name = "LocalProvisioningFailure";
  retryable;
  constructor(code) {
    super(code);
    this.code = code;
    this.retryable = RETRYABLE.has(code);
  }
};

// node_modules/@nexusaos/local-provisioning-electron-ble/dist/index.js
var GATT_FLAG_START = 1;
var GATT_FLAG_END = 2;
var protocol = () => {
  throw new LocalProvisioningFailure("GATT_FRAGMENT_PROTOCOL_ERROR");
};
function fragmentLogicalFrame(frame, messageId) {
  if (!(frame instanceof Uint8Array) || frame.length < 2 || frame.length > GATT_MAX_LOGICAL_BYTES || frame[frame.length - 1] !== 10 || !Number.isInteger(messageId) || messageId < 1 || messageId > 65535)
    protocol();
  const count = Math.ceil(frame.length / GATT_PAYLOAD_BYTES);
  if (count < 1 || count > GATT_MAX_FRAGMENTS)
    protocol();
  const output = [];
  for (let index = 0; index < count; index++) {
    const payload = frame.slice(index * GATT_PAYLOAD_BYTES, (index + 1) * GATT_PAYLOAD_BYTES);
    const value = new Uint8Array(GATT_HEADER_BYTES + payload.length);
    const view = new DataView(value.buffer);
    value[0] = 2;
    value[1] = (index === 0 ? GATT_FLAG_START : 0) | (index === count - 1 ? GATT_FLAG_END : 0);
    view.setUint16(2, messageId);
    view.setUint16(4, index);
    view.setUint16(6, count);
    value.set(payload, 8);
    output.push(value);
  }
  return output;
}
function createGattReassembler(clock2, timeoutMs = 5e3) {
  let id = 0, count = 0, next = 0, started = 0, lastAt = 0, total = 0, chunks = [];
  const reset = () => {
    id = count = next = started = lastAt = total = 0;
    chunks = [];
  };
  const timeout = () => {
    const now = clock2.monotonicMs();
    if (next > 0 && (now - lastAt >= timeoutMs || now - started >= 3e4)) {
      reset();
      throw new LocalProvisioningFailure("GATT_FRAGMENT_INCOMPLETE");
    }
  };
  return {
    get pending() {
      return next > 0;
    },
    reset,
    checkTimeout: timeout,
    push(fragment) {
      timeout();
      if (!(fragment instanceof Uint8Array) || fragment.length < GATT_HEADER_BYTES || fragment.length > 20)
        protocol();
      const view = new DataView(fragment.buffer, fragment.byteOffset, fragment.byteLength);
      const version = fragment[0], flags = fragment[1], messageId = view.getUint16(2), index = view.getUint16(4), fragmentCount = view.getUint16(6);
      if (version !== 2 || (flags & ~3) !== 0 || messageId < 1 || fragmentCount < 1 || fragmentCount > GATT_MAX_FRAGMENTS || index >= fragmentCount || fragment.length === 8)
        protocol();
      if (next === 0) {
        if (index !== 0 || (flags & GATT_FLAG_START) === 0) {
          protocol();
        }
        id = messageId;
        count = fragmentCount;
        started = lastAt = clock2.monotonicMs();
      } else if (messageId !== id || fragmentCount !== count || index !== next || (flags & GATT_FLAG_START) !== 0)
        protocol();
      const last = index === fragmentCount - 1;
      if (last !== Boolean(flags & GATT_FLAG_END) || !last && fragment.length !== 20)
        protocol();
      const payload = fragment.slice(8);
      total += payload.length;
      if (total > GATT_MAX_LOGICAL_BYTES)
        protocol();
      chunks.push(payload);
      next++;
      lastAt = clock2.monotonicMs();
      if (!last)
        return void 0;
      const result = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        result.set(chunk, offset);
        offset += chunk.length;
      }
      reset();
      if (result[result.length - 1] !== 10)
        protocol();
      return result;
    }
  };
}
function createMessageIdAllocator() {
  let cursor = 0;
  const pending = /* @__PURE__ */ new Set();
  const recent = /* @__PURE__ */ new Map();
  return {
    next(now) {
      for (const [id, at] of recent)
        if (now - at >= 3e4)
          recent.delete(id);
      for (let tries = 0; tries < 65535; tries++) {
        cursor = cursor === 65535 ? 1 : cursor + 1;
        if (!pending.has(cursor) && !recent.has(cursor))
          return cursor;
      }
      return protocol();
    },
    markPending(id) {
      if (!Number.isInteger(id) || id < 1 || id > 65535 || pending.has(id) || recent.has(id))
        protocol();
      pending.add(id);
    },
    release(id, now) {
      if (!pending.delete(id))
        protocol();
      recent.set(id, now);
    },
    clear() {
      pending.clear();
      recent.clear();
      cursor = 0;
    }
  };
}
var ELECTRON_GATT_V2_PROFILE = Object.freeze({
  serviceUuid: PROVISIONING_SERVICE_UUID,
  deviceInfoUuid: DEVICE_INFO_UUID,
  commandUuid: COMMAND_UUID,
  statusUuid: STATUS_UUID,
  namePrefix: SETUP_NAME_PREFIX,
  deviceInfoMaxBytes: 512,
  logicalFrameMaxBytes: GATT_MAX_LOGICAL_BYTES,
  attributeMaxBytes: 20,
  writeMode: "with-response",
  statusMode: "notify-only"
});
function createElectronBluetoothSelectionEndpoint(input) {
  const randomId = input.randomId ?? (() => crypto.randomUUID());
  const leaseMs = input.leaseMs ?? 6e4;
  let selected;
  return {
    async requestDeviceFromUserGesture() {
      selected = void 0;
      const device = await input.bluetooth.requestDevice({
        filters: [
          {
            services: [PROVISIONING_SERVICE_UUID],
            namePrefix: SETUP_NAME_PREFIX
          }
        ],
        optionalServices: [PROVISIONING_SERVICE_UUID]
      });
      if (!device.name?.startsWith(SETUP_NAME_PREFIX))
        throw new LocalProvisioningFailure("LOCAL_SELECTION_LEASE_INVALID");
      const now = input.clock.monotonicMs(), handle = randomId(), lease = randomId();
      const candidate = {
        candidateId: handle,
        transport: "ble-gatt",
        transportHandle: handle,
        advertisedName: device.name,
        rssi: null,
        firstSeenMonotonicMs: now,
        lastSeenMonotonicMs: now,
        selectionLeaseId: lease
      };
      selected = { candidate, device, expiresAt: now + leaseMs };
      return candidate;
    },
    async consumeSelectionLease(candidate) {
      const current = selected;
      selected = void 0;
      if (!current || input.clock.monotonicMs() > current.expiresAt || candidate.transport !== "ble-gatt" || candidate.selectionLeaseId !== current.candidate.selectionLeaseId || candidate.transportHandle !== current.candidate.transportHandle || candidate.candidateId !== current.candidate.candidateId)
        throw new LocalProvisioningFailure("LOCAL_SELECTION_LEASE_INVALID");
      return current.device;
    },
    cancelSelection() {
      selected = void 0;
    }
  };
}
var bytes = (view) => new Uint8Array(view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength));
var route = (frame) => {
  if (frame.length < 2 || frame[frame.length - 1] !== 10)
    protocol();
  let value;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(frame.subarray(0, -1)));
  } catch {
    protocol();
  }
  if (!value || typeof value !== "object" || Array.isArray(value))
    protocol();
  const envelope = value;
  if (typeof envelope.request_id !== "string" || typeof envelope.message_type !== "string")
    protocol();
  return {
    requestId: envelope.request_id,
    messageType: envelope.message_type
  };
};
function createElectronGattTransportFactory(input) {
  let selected;
  return {
    async connect(candidate, signal) {
      if (signal.aborted)
        throw new DOMException("Aborted", "AbortError");
      const device = selected && selected.candidateId === candidate.candidateId && selected.transportHandle === candidate.transportHandle && selected.selectionLeaseId === candidate.selectionLeaseId ? selected.device : await input.consumeSelectionLease(candidate);
      selected = {
        candidateId: candidate.candidateId,
        transportHandle: candidate.transportHandle,
        selectionLeaseId: candidate.selectionLeaseId,
        device
      };
      if (!device.gatt)
        throw new LocalProvisioningFailure("GATT_SERVICE_NOT_FOUND");
      let server, service, deviceInfo, command, status;
      try {
        server = await device.gatt.connect();
        service = await server.getPrimaryService(PROVISIONING_SERVICE_UUID);
        [deviceInfo, command, status] = await Promise.all([
          service.getCharacteristic(DEVICE_INFO_UUID),
          service.getCharacteristic(COMMAND_UUID),
          service.getCharacteristic(STATUS_UUID)
        ]);
      } catch {
        device.gatt.disconnect();
        throw new LocalProvisioningFailure("GATT_CHARACTERISTIC_MISMATCH");
      }
      const allocator = createMessageIdAllocator(), reassemblers = /* @__PURE__ */ new Map(), completed = /* @__PURE__ */ new Map();
      let closed = false, pending, statusListener;
      const closeWith = (error) => {
        if (closed)
          return;
        closed = true;
        if (pending) {
          clearTimeout(pending.timer);
          pending.reject(error ?? new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED"));
          pending = void 0;
        }
        status.removeEventListener("characteristicvaluechanged", onStatus);
        device.removeEventListener("gattserverdisconnected", onDisconnect);
        void status.stopNotifications().catch(() => void 0);
        server.disconnect();
        allocator.clear();
        reassemblers.clear();
      };
      const onDisconnect = () => closeWith(new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED"));
      const onStatus = (event) => {
        try {
          const characteristic = event.target, value = characteristic.value;
          if (!value)
            protocol();
          const fragment = bytes(value), id = new DataView(fragment.buffer, fragment.byteOffset, fragment.byteLength).getUint16(2);
          let reassembler = reassemblers.get(id);
          if (!reassembler) {
            reassembler = createGattReassembler(input.clock);
            reassemblers.set(id, reassembler);
          }
          const frame = reassembler.push(fragment);
          if (!frame)
            return;
          reassemblers.delete(id);
          const outer = route(frame), key = `${outer.requestId}
${outer.messageType}`, now = input.clock.monotonicMs();
          for (const [oldKey, at] of completed)
            if (now - at >= 3e4)
              completed.delete(oldKey);
          if (completed.has(key))
            return;
          if (pending && pending.key === key) {
            const current = pending;
            pending = void 0;
            clearTimeout(current.timer);
            completed.set(key, now);
            current.resolve(frame);
            return;
          }
          if (outer.messageType === "job.status.event" && statusListener) {
            completed.set(key, now);
            statusListener(frame);
            return;
          }
          protocol();
        } catch (error) {
          closeWith(error);
        }
      };
      device.addEventListener("gattserverdisconnected", onDisconnect);
      status.addEventListener("characteristicvaluechanged", onStatus);
      try {
        await status.startNotifications();
      } catch {
        closeWith();
        throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
      }
      const transport2 = {
        kind: "ble-gatt",
        capabilities: { statusNotifications: true },
        async readDeviceInfo(readSignal) {
          if (closed)
            throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
          if (readSignal.aborted)
            throw new DOMException("Aborted", "AbortError");
          const result = bytes(await deviceInfo.readValue());
          if (result.length < 2 || result.length > 512)
            throw new LocalProvisioningFailure("LOCAL_DEVICE_INFO_INVALID");
          return result;
        },
        async request(logicalFrame, match, requestSignal) {
          if (closed)
            throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
          if (pending)
            throw new LocalProvisioningFailure("GATT_FRAGMENT_PROTOCOL_ERROR");
          if (requestSignal.aborted)
            throw new DOMException("Aborted", "AbortError");
          const id = allocator.next(input.clock.monotonicMs());
          const requestKey = `${match.requestId}
${match.responseMessageType}`;
          allocator.markPending(id);
          const response = new Promise((resolve, reject) => {
            pending = {
              key: requestKey,
              resolve,
              reject,
              timer: setTimeout(() => {
                if (pending?.key === requestKey) {
                  const current = pending;
                  pending = void 0;
                  completed.set(requestKey, input.clock.monotonicMs());
                  const failure2 = new LocalProvisioningFailure("GATT_FRAGMENT_INCOMPLETE");
                  current.reject(failure2);
                  closeWith(failure2);
                }
              }, input.requestTimeoutMs ?? 5e3)
            };
          });
          const abort = () => {
            if (pending?.key === requestKey) {
              const current = pending;
              pending = void 0;
              clearTimeout(current.timer);
              completed.set(requestKey, input.clock.monotonicMs());
              current.reject(new DOMException("Aborted", "AbortError"));
            }
          };
          requestSignal.addEventListener("abort", abort, { once: true });
          try {
            for (const fragment of fragmentLogicalFrame(logicalFrame, id)) {
              if (requestSignal.aborted)
                throw new DOMException("Aborted", "AbortError");
              await command.writeValueWithResponse(fragment.slice().buffer);
            }
            return await response;
          } catch (error) {
            const current = pending;
            if (current?.key === requestKey) {
              pending = void 0;
              clearTimeout(current.timer);
              completed.set(requestKey, input.clock.monotonicMs());
              current.reject(error);
              await response.catch(() => void 0);
            }
            if (error instanceof DOMException && error.name === "AbortError")
              throw error;
            const failure2 = error instanceof LocalProvisioningFailure ? error : new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
            closeWith(failure2);
            throw failure2;
          } finally {
            requestSignal.removeEventListener("abort", abort);
            if (!closed)
              allocator.release(id, input.clock.monotonicMs());
          }
        },
        async subscribeStatus(listener, subscribeSignal) {
          if (closed)
            throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
          if (subscribeSignal.aborted)
            throw new DOMException("Aborted", "AbortError");
          statusListener = listener;
          const clear = () => {
            if (statusListener === listener)
              statusListener = void 0;
          };
          subscribeSignal.addEventListener("abort", clear, { once: true });
          return async () => {
            subscribeSignal.removeEventListener("abort", clear);
            clear();
          };
        },
        async close() {
          closeWith();
        }
      };
      return transport2;
    }
  };
}
function createElectronGattTransportEndpoint(input) {
  const connections = /* @__PURE__ */ new Map(), randomId = input.randomId ?? (() => crypto.randomUUID());
  const get = (id) => {
    const transport2 = connections.get(id);
    if (!transport2)
      throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
    return transport2;
  };
  return {
    async connect(candidate, signal) {
      const transport2 = await input.factory.connect(candidate, signal), id = randomId();
      connections.set(id, transport2);
      return id;
    },
    readDeviceInfo(id, signal) {
      return get(id).readDeviceInfo(signal);
    },
    request(id, frame, match, signal) {
      return get(id).request(frame, match, signal);
    },
    async subscribeStatus(id, listener, signal) {
      const subscribe = get(id).subscribeStatus;
      if (!subscribe)
        throw new LocalProvisioningFailure("LOCAL_TRANSPORT_DISCONNECTED");
      return subscribe.call(get(id), listener, signal);
    },
    async close(id) {
      const transport2 = connections.get(id);
      connections.delete(id);
      await transport2?.close();
    },
    async closeAll() {
      const all = [...connections.values()];
      connections.clear();
      await Promise.all(all.map((transport2) => transport2.close()));
    }
  };
}

// provisioning/preload-formal.mjs
var CLAIM_INVOKE_CHANNEL = "zhijun:provisioning:claim:v1";
var CLAIM_SNAPSHOT_CHANNEL = "zhijun:provisioning:snapshot:v1";
var TRANSPORT_COMMAND_CHANNEL = "zhijun:provisioning:transport-command:v1";
var TRANSPORT_RESULT_CHANNEL = "zhijun:provisioning:transport-result:v1";
var IPC_VERSION = 1;
var FLOW_ARGUMENT = "--zhijun-provisioning-flow=";
var UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
var TRANSPORT_ERRORS = /* @__PURE__ */ new Set([
  "LOCAL_CONNECT_TIMEOUT",
  "LOCAL_CONNECT_UNSUPPORTED_BOND_FLOW",
  "LOCAL_SELECTION_LEASE_INVALID",
  "LOCAL_DEVICE_INFO_INVALID",
  "LOCAL_DEVICE_INCOMPATIBLE",
  "GATT_SERVICE_NOT_FOUND",
  "GATT_CHARACTERISTIC_MISMATCH",
  "GATT_FRAGMENT_PROTOCOL_ERROR",
  "GATT_FRAGMENT_INCOMPLETE",
  "LOCAL_TRANSPORT_DISCONNECTED",
  "LOCAL_USER_CANCELLED"
]);
var CLAIM_ERRORS = /* @__PURE__ */ new Set([
  "CLAIM_INVALID_STATE",
  "CLAIM_OPERATION_IN_PROGRESS",
  "CLAIM_INPUT_MISMATCH",
  "AUTH_REQUIRED",
  "AUTH_SESSION_EXPIRED",
  "CLIENT_REVOKED",
  "CLIENT_KEY_INVALID",
  "CLIENT_KEY_MISMATCH",
  "ACCOUNT_NOT_ACTIVE",
  "CLIENT_UPGRADE_REQUIRED",
  "REQUEST_SIGNATURE_INVALID",
  "REQUEST_EXPIRED",
  "NONCE_REPLAYED",
  "IDEMPOTENCY_CONFLICT",
  "DEVICE_IDENTITY_MISMATCH",
  "DEVICE_NOT_ENROLLED",
  "DEVICE_DISABLED",
  "DEVICE_NOT_CLAIMABLE",
  "DEVICE_ALREADY_OWNED",
  "PAIRING_NOT_FOUND",
  "PAIRING_EXPIRED",
  "PAIRING_CANCELLED",
  "PAIRING_MISMATCH",
  "PAIRING_SUPERSEDED",
  "SETUP_WINDOW_CLOSED",
  "ACCESS_PROJECTION_PENDING",
  "ACCESS_PROJECTION_FAILED",
  "DEVICE_ACK_TIMEOUT",
  "DEVICE_OFFLINE",
  "FEATURE_NOT_AVAILABLE",
  "SERVICE_TEMPORARILY_UNAVAILABLE",
  "VERIFICATION_CODE_MISMATCH",
  "UNSUPPORTED_NETWORK_SECURITY",
  "HELLO_CONTEXT_EXPIRED",
  "REQUESTED_OPS_MISMATCH",
  "PROTOCOL_CHANGED",
  "CLAIM_UNKNOWN_ERROR"
]);
var LOCAL_UI_ERRORS = /* @__PURE__ */ new Set([
  "PROVISIONING_USER_GESTURE_REQUIRED",
  "PROVISIONING_BLUETOOTH_UNAVAILABLE",
  "PROVISIONING_SCAN_IN_PROGRESS",
  "PROVISIONING_PHYSICAL_CODE_INVALID",
  "PROVISIONING_NOT_STARTED",
  "DISCOVERY_RUNTIME_FAILURE",
  "PROVISIONING_SCAN_TIMEOUT",
  "PROVISIONING_SCAN_CANCELLED",
  "NotFoundError",
  "NotAllowedError",
  "SecurityError"
]);
var flowId = process.argv.find((value) => value.startsWith(FLOW_ARGUMENT))?.slice(FLOW_ARGUMENT.length);
if (!UUID_V4.test(flowId || "")) throw new Error("PROVISIONING_CONFIGURATION_INVALID");
var clock = Object.freeze({ monotonicMs: () => performance.now() });
var selection = createElectronBluetoothSelectionEndpoint({ bluetooth: navigator.bluetooth, clock });
var factory = createElectronGattTransportFactory({ consumeSelectionLease: (candidate) => selection.consumeSelectionLease(candidate), clock });
var transport = createElectronGattTransportEndpoint({ factory });
var candidates = /* @__PURE__ */ new Map();
var candidateWaiters = /* @__PURE__ */ new Set();
var operations = /* @__PURE__ */ new Map();
var subscriptions = /* @__PURE__ */ new Map();
var snapshotListeners = /* @__PURE__ */ new Set();
var discoveryListeners = /* @__PURE__ */ new Set();
var retiredDiscoverySessions = /* @__PURE__ */ new Set();
var selectedCandidate;
var requestDeviceFlight;
var scanActive = false;
var discoverySessionId;
var discoverySequence = 0;
var discoveryGeneration = 0;
var discoveryTimer;
var discoveryState = "idle";
var discoveryErrorCode;
var selecting = false;
function uuid() {
  return crypto.randomUUID();
}
function safeCode(error, fallback = "CLAIM_UNKNOWN_ERROR") {
  for (const value of [error?.code, error?.name, error?.message]) {
    if (typeof value !== "string") continue;
    for (const code of [...TRANSPORT_ERRORS, ...CLAIM_ERRORS, ...LOCAL_UI_ERRORS]) {
      if (value === code || value.endsWith(`: ${code}`)) return code;
    }
  }
  return fallback;
}
function failure(code) {
  const error = new Error(code);
  error.name = code;
  error.code = code;
  return error;
}
function safeSnapshot(value) {
  const states = /* @__PURE__ */ new Set([
    "idle",
    "authenticating",
    "awaitingWifi",
    "awaitingOwnershipConfirmation",
    "waitingCloud",
    "attentionRequired",
    "completed",
    "cancelled",
    "restartRequired",
    "terminalError"
  ]);
  if (!value || typeof value !== "object" || !states.has(value.state)) return { state: "attentionRequired" };
  return Object.freeze({
    state: value.state,
    ...typeof value.attentionCode === "string" && /^[A-Z][A-Z0-9_]{1,127}$/.test(value.attentionCode) ? { attentionCode: value.attentionCode } : {}
  });
}
function candidateSnapshot() {
  return Object.freeze([...candidates.values()].map((candidate) => Object.freeze({ ...candidate })));
}
function discoverySnapshot() {
  return Object.freeze({
    state: discoveryState,
    candidates: candidateSnapshot(),
    ...discoveryErrorCode ? { errorCode: discoveryErrorCode } : {}
  });
}
function notifyDiscovery() {
  const snapshot = discoverySnapshot();
  for (const listener of [...discoveryListeners]) {
    try {
      listener(snapshot);
    } catch {
    }
  }
}
function notifyCandidates(error) {
  const snapshot = candidateSnapshot();
  for (const waiter of [...candidateWaiters]) {
    if (error) waiter.reject(error);
    else waiter.resolve(snapshot);
  }
  candidateWaiters.clear();
}
function finishDiscovery(state, errorCode, cancelPicker = false) {
  clearTimeout(discoveryTimer);
  discoveryTimer = void 0;
  scanActive = false;
  selecting = false;
  discoveryGeneration += 1;
  const sessionId = discoverySessionId;
  if (sessionId) retiredDiscoverySessions.add(sessionId);
  discoverySessionId = void 0;
  discoverySequence = 0;
  discoveryState = state;
  discoveryErrorCode = errorCode;
  if (cancelPicker) {
    selection.cancelSelection();
    if (sessionId) import_electron.ipcRenderer.send(DISCOVERY_PICKER_CHANNELS.cancel, { sessionId });
  }
  notifyCandidates(errorCode ? failure(errorCode) : void 0);
  notifyDiscovery();
}
import_electron.ipcRenderer.on(DISCOVERY_PICKER_CHANNELS.candidates, (_event, message) => {
  if (!message || typeof message !== "object" || !UUID_V4.test(message.sessionId || "") || !Number.isSafeInteger(message.sequence) || message.sequence < 1 || !Array.isArray(message.candidates)) return;
  if (retiredDiscoverySessions.has(message.sessionId)) return;
  if (!scanActive) {
    retiredDiscoverySessions.add(message.sessionId);
    import_electron.ipcRenderer.send(DISCOVERY_PICKER_CHANNELS.cancel, { sessionId: message.sessionId });
    return;
  }
  if (discoverySessionId !== message.sessionId) {
    if (discoverySessionId) return;
    discoverySessionId = message.sessionId;
    discoverySequence = 0;
    candidates.clear();
  }
  if (message.sequence <= discoverySequence) return;
  discoverySequence = message.sequence;
  const next = /* @__PURE__ */ new Set();
  for (const device of message.candidates.slice(0, 20)) {
    if (!UUID_V4.test(device?.candidateId || "") || device.transport !== "ble-gatt" || typeof device?.advertisedName !== "string") continue;
    const candidateId = device.candidateId;
    const name = device.advertisedName.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f]/gu, "").trim().slice(0, 64) || "\u9644\u8FD1\u7684 AI \u76D2\u5B50";
    const suffix = /^CentaurOS-Setup-([A-Za-z0-9]{6})$/.exec(name)?.[1];
    candidates.set(candidateId, { candidateId, name, ...suffix ? { shortCode: suffix } : {} });
    next.add(candidateId);
  }
  for (const id of [...candidates.keys()]) if (!next.has(id)) candidates.delete(id);
  notifyDiscovery();
  if (candidates.size) notifyCandidates();
});
import_electron.ipcRenderer.on(DISCOVERY_PICKER_CHANNELS.settled, (_event, message) => {
  if (!message || message.sessionId !== discoverySessionId || message.cancelled !== true) return;
  finishDiscovery("completed", candidates.size ? void 0 : "PROVISIONING_SCAN_TIMEOUT", true);
});
import_electron.ipcRenderer.on(CLAIM_SNAPSHOT_CHANNEL, (_event, message) => {
  if (!message || message.ipcVersion !== IPC_VERSION || message.flowId !== flowId) return;
  const snapshot = safeSnapshot(message.snapshot);
  for (const listener of [...snapshotListeners]) listener(snapshot);
});
function sendTransport(message) {
  import_electron.ipcRenderer.send(TRANSPORT_RESULT_CHANNEL, { ipcVersion: IPC_VERSION, flowId, ...message });
}
function bytes2(value) {
  if (!(value instanceof ArrayBuffer) && !ArrayBuffer.isView(value)) throw failure("GATT_FRAGMENT_PROTOCOL_ERROR");
  const source = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (source.byteLength < 2 || source.byteLength > 2048) throw failure("GATT_FRAGMENT_PROTOCOL_ERROR");
  return source.slice();
}
import_electron.ipcRenderer.on(TRANSPORT_COMMAND_CHANNEL, (_event, message) => {
  if (!message || message.ipcVersion !== IPC_VERSION || message.flowId !== flowId || !UUID_V4.test(message.operationId || "") || typeof message.kind !== "string") return;
  if (message.kind === "transport.abort") {
    operations.get(message.targetOperationId)?.abort();
    sendTransport({
      kind: "transport.aborted",
      operationId: message.operationId,
      targetOperationId: message.targetOperationId
    });
    return;
  }
  const controller = new AbortController();
  operations.set(message.operationId, controller);
  void (async () => {
    if (message.kind === "transport.connect") {
      if (!selectedCandidate || message.selectionLeaseId !== selectedCandidate.selectionLeaseId) {
        throw failure("LOCAL_SELECTION_LEASE_INVALID");
      }
      const candidate = selectedCandidate;
      selectedCandidate = void 0;
      const connectionId = await transport.connect(candidate, controller.signal);
      sendTransport({ kind: "transport.connected", operationId: message.operationId, connectionId });
    } else if (message.kind === "transport.readDeviceInfo") {
      const value = await transport.readDeviceInfo(message.connectionId, controller.signal);
      sendTransport({
        kind: "transport.deviceInfo",
        operationId: message.operationId,
        connectionId: message.connectionId,
        bytes: value.slice().buffer
      });
    } else if (message.kind === "transport.request") {
      const value = await transport.request(
        message.connectionId,
        bytes2(message.logicalFrame),
        { requestId: message.requestId, responseMessageType: message.responseMessageType },
        controller.signal
      );
      sendTransport({
        kind: "transport.response",
        operationId: message.operationId,
        connectionId: message.connectionId,
        logicalFrame: value.slice().buffer
      });
    } else if (message.kind === "transport.subscribeStatus") {
      let sequence = 0;
      const subscriptionId = uuid();
      const unsubscribe = await transport.subscribeStatus(message.connectionId, (logicalFrame) => {
        sendTransport({
          kind: "transport.status",
          connectionId: message.connectionId,
          subscriptionId,
          sequence: ++sequence,
          logicalFrame: logicalFrame.slice().buffer
        });
      }, controller.signal);
      subscriptions.set(`${message.connectionId}:${subscriptionId}`, unsubscribe);
      sendTransport({
        kind: "transport.subscribed",
        operationId: message.operationId,
        connectionId: message.connectionId,
        subscriptionId
      });
    } else if (message.kind === "transport.unsubscribeStatus") {
      const key = `${message.connectionId}:${message.subscriptionId}`;
      const unsubscribe = subscriptions.get(key);
      subscriptions.delete(key);
      await unsubscribe?.();
      sendTransport({
        kind: "transport.unsubscribed",
        operationId: message.operationId,
        connectionId: message.connectionId,
        subscriptionId: message.subscriptionId
      });
    } else if (message.kind === "transport.close") {
      for (const [key, unsubscribe] of [...subscriptions]) {
        if (key.startsWith(`${message.connectionId}:`)) {
          subscriptions.delete(key);
          await unsubscribe().catch(() => {
          });
        }
      }
      await transport.close(message.connectionId);
      sendTransport({
        kind: "transport.closed",
        operationId: message.operationId,
        connectionId: message.connectionId,
        reason: "requested"
      });
    } else throw failure("GATT_FRAGMENT_PROTOCOL_ERROR");
  })().catch((error) => sendTransport({
    kind: "transport.failure",
    operationId: message.operationId,
    code: safeCode(error, "LOCAL_TRANSPORT_DISCONNECTED"),
    retryable: error?.retryable === true
  })).finally(() => operations.delete(message.operationId));
});
async function claim(action, input) {
  try {
    return await import_electron.ipcRenderer.invoke(CLAIM_INVOKE_CHANNEL, {
      ipcVersion: IPC_VERSION,
      flowId,
      operationId: uuid(),
      kind: `claim.${action}`,
      ...input === void 0 ? {} : { input }
    });
  } catch (error) {
    throw failure(safeCode(error));
  }
}
var api = Object.freeze({
  available: Boolean(navigator.bluetooth?.requestDevice),
  testBuild: false,
  legacyAllowed: false,
  async scan() {
    if (scanActive || requestDeviceFlight) throw failure("CLAIM_OPERATION_IN_PROGRESS");
    if (!navigator.userActivation?.isActive) throw failure("PROVISIONING_USER_GESTURE_REQUIRED");
    if (!navigator.bluetooth?.requestDevice) throw failure("PROVISIONING_BLUETOOTH_UNAVAILABLE");
    scanActive = true;
    candidates.clear();
    selectedCandidate = void 0;
    discoverySessionId = void 0;
    discoverySequence = 0;
    discoveryState = "scanning";
    discoveryErrorCode = void 0;
    const generation = ++discoveryGeneration;
    const result = new Promise((resolve, reject) => candidateWaiters.add({ resolve, reject }));
    try {
      const pending = selection.requestDeviceFromUserGesture();
      requestDeviceFlight = pending;
      pending.then(() => {
        if (requestDeviceFlight === pending) requestDeviceFlight = void 0;
        if (generation !== discoveryGeneration) selection.cancelSelection();
      }, (error) => {
        if (requestDeviceFlight === pending) requestDeviceFlight = void 0;
        if (generation !== discoveryGeneration) return;
        const code = safeCode(error, "DISCOVERY_RUNTIME_FAILURE");
        finishDiscovery(
          code === "NotFoundError" ? "completed" : "error",
          code === "NotFoundError" && candidates.size ? void 0 : code,
          true
        );
      });
      discoveryTimer = setTimeout(() => {
        if (generation === discoveryGeneration) {
          finishDiscovery("completed", candidates.size ? void 0 : "PROVISIONING_SCAN_TIMEOUT", true);
        }
      }, 9e4);
      notifyDiscovery();
    } catch (error) {
      finishDiscovery("error", safeCode(error, "DISCOVERY_RUNTIME_FAILURE"), true);
    }
    return result;
  },
  async select(candidateId) {
    const candidate = candidates.get(candidateId);
    if (!candidate || !scanActive || selecting || !requestDeviceFlight || !discoverySessionId) {
      throw failure("LOCAL_SELECTION_LEASE_INVALID");
    }
    selecting = true;
    const generation = discoveryGeneration;
    const pending = requestDeviceFlight;
    import_electron.ipcRenderer.send(
      DISCOVERY_PICKER_CHANNELS.select,
      { sessionId: discoverySessionId, candidateId: candidate.candidateId }
    );
    let bound;
    try {
      bound = await pending;
    } catch (error) {
      throw failure(safeCode(error, "DISCOVERY_RUNTIME_FAILURE"));
    }
    if (generation !== discoveryGeneration) throw failure("LOCAL_SELECTION_LEASE_INVALID");
    selectedCandidate = bound;
    finishDiscovery("selected");
    await claim("bindSelected", bound);
  },
  begin: () => claim("begin"),
  confirmPhysicalDevice(code) {
    if (typeof code !== "string" || !/^\d{6}$/.test(code)) return Promise.reject(failure("VERIFICATION_CODE_MISMATCH"));
    return claim("confirmPhysicalDevice", { verificationCode: code });
  },
  async provideWifi(input) {
    if (!input || typeof input.ssid !== "string" || !["open", "wpa-personal"].includes(input.security) || typeof input.password !== "string") throw failure("CLAIM_INPUT_MISMATCH");
    const security = input.security === "wpa-personal" ? "wpa_personal" : "open";
    const encoded = security === "wpa_personal" ? new TextEncoder().encode(input.password) : void 0;
    try {
      return await claim("provideWifi", {
        ssid: input.ssid,
        security,
        hidden: false,
        ...encoded ? { passwordUtf8: encoded.buffer } : {}
      });
    } finally {
      encoded?.fill(0);
    }
  },
  confirmOwnership: () => claim("confirmOwnership"),
  refresh: () => claim("refresh"),
  cancel: () => {
    if (scanActive) finishDiscovery("cancelled", "PROVISIONING_SCAN_CANCELLED", true);
    return claim("cancel");
  },
  cancelScan: () => {
    if (scanActive) finishDiscovery("cancelled", "PROVISIONING_SCAN_CANCELLED", true);
  },
  onDiscoverySnapshot(listener) {
    if (typeof listener !== "function") throw new TypeError("Expected a discovery listener");
    discoveryListeners.add(listener);
    listener(discoverySnapshot());
    return () => discoveryListeners.delete(listener);
  },
  onSnapshot(listener) {
    if (typeof listener !== "function") throw new TypeError("Expected a snapshot listener");
    snapshotListeners.add(listener);
    let active = true;
    void claim("snapshot").then((snapshot) => {
      if (active) listener(safeSnapshot(snapshot));
    }).catch(() => {
    });
    return () => {
      active = false;
      snapshotListeners.delete(listener);
    };
  }
});
addEventListener("beforeunload", () => {
  if (scanActive) finishDiscovery("cancelled", "PROVISIONING_SCAN_CANCELLED", true);
  else selection.cancelSelection();
  for (const operation of operations.values()) operation.abort();
  operations.clear();
  subscriptions.clear();
  snapshotListeners.clear();
  discoveryListeners.clear();
  void transport.closeAll();
});
import_electron.contextBridge.exposeInMainWorld("desktopProvisioning", api);
