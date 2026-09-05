"use strict";
/**
 * BLE Command 分片/重组（WP N）。
 *
 * 指南要求：每个 GATT 分片按 vector 逐字节通过；异常分片、断线和超时不会解析
 * 半帧或降级协议。本模块是纯函数分片器：帧头 {seq, total, crc} + 载荷 hex，
 * 重组时严格校验序号连续、帧数一致、CRC 与长度上限，任一不符即抛错拒绝，
 * 不产出半帧。不 import electron，可在 node 环境直接单测。
 *
 * 帧格式（骨架版，阶段 4 以合同 vectors 为准）：
 *   { seq, total, payloadHex }
 *   crc = 载荷 hex 的 8 位和（取模 256），接收端重算比对。
 */
const { COMMAND_MAX_FRAME_PAYLOAD, COMMAND_MAX_TOTAL_FRAMES } = require("./ble-contracts.js");

function toHexChunks(payloadHex, chunkSize) {
  const chunks = [];
  for (let i = 0; i < payloadHex.length; i += chunkSize) {
    chunks.push(payloadHex.slice(i, i + chunkSize));
  }
  return chunks;
}

function crc8Hex(hex) {
  let sum = 0;
  for (let i = 0; i < hex.length; i += 2) {
    const byte = parseInt(hex.slice(i, i + 2), 16);
    if (Number.isNaN(byte)) throw new Error("分片载荷不是合法 hex");
    sum = (sum + byte) & 0xff;
  }
  return sum;
}

/**
 * 将整条命令载荷（hex 字符串）切成带序号/总数/校验的帧列表。
 * 空载荷、超长帧数或非法 hex 一律拒绝。
 */
function frameCommand(payloadHex, { chunkSize = COMMAND_MAX_FRAME_PAYLOAD, maxTotal = COMMAND_MAX_TOTAL_FRAMES } = {}) {
  if (typeof payloadHex !== "string" || payloadHex.length === 0 || payloadHex.length % 2 !== 0) {
    throw new Error("命令载荷必须是非空偶数长度 hex");
  }
  if (!Number.isInteger(chunkSize) || chunkSize <= 0 || chunkSize % 2 !== 0) {
    throw new Error("分片大小必须是正偶数");
  }
  crc8Hex(payloadHex);
  const chunks = toHexChunks(payloadHex, chunkSize);
  if (chunks.length > maxTotal) {
    throw new Error(`命令超过最大分片数（${chunks.length} > ${maxTotal}）`);
  }
  return chunks.map((chunk, index) => ({
    seq: index,
    total: chunks.length,
    payloadHex: chunk,
    crc: crc8Hex(chunk),
  }));
}

/**
 * 重组帧列表为完整载荷 hex；校验序号连续（从 0 起）、总数一致、
 * 每帧 CRC 正确。任何不符抛错，绝不返回半帧。
 */
function assembleFrames(frames) {
  if (!Array.isArray(frames) || frames.length === 0) {
    throw new Error("帧列表为空，无法重组");
  }
  const total = frames[0].total;
  if (!Number.isInteger(total) || total <= 0 || total > COMMAND_MAX_TOTAL_FRAMES) {
    throw new Error("帧总数非法");
  }
  const ordered = [];
  for (const frame of frames) {
    if (!frame || frame.total !== total || !Number.isInteger(frame.seq)) {
      throw new Error("帧元数据不一致（total 或 seq 非法）");
    }
    if (frame.seq < 0 || frame.seq >= total) {
      throw new Error(`帧序号越界：${frame.seq}`);
    }
    if (typeof frame.payloadHex !== "string" || frame.payloadHex.length % 2 !== 0) {
      throw new Error("帧载荷不是偶数长度 hex");
    }
    if (crc8Hex(frame.payloadHex) !== frame.crc) {
      throw new Error(`帧 ${frame.seq} CRC 校验失败`);
    }
    ordered[frame.seq] = frame.payloadHex;
  }
  for (let i = 0; i < total; i += 1) {
    if (ordered[i] === undefined) {
      throw new Error(`帧缺失：seq=${i}`);
    }
  }
  return ordered.join("");
}

module.exports = { frameCommand, assembleFrames, crc8Hex };
