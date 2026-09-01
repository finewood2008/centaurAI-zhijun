"use strict";
/**
 * Main 安全存储与 Client Key（WP M）。
 *
 * 只允许在 Electron Main 进程使用：秘密（Client 私钥、rootSecret、Token）仅经本模块
 * 短暂处理，落盘一律用 safeStorage 加密，绝不写入普通 Storage 或日志。
 * safeStorage 不可用（如 Linux 无 keyring 且未启用 basic_text）时 fail-closed：
 * 拒绝读写密钥，调用方不得降级为明文存储。
 *
 * 依赖注入：构造时传入 { userDataDir, safeStorage }，便于单测 mock electron。
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const CLIENT_KEY_FILE = "client-key.pem.enc";

class SecureStore {
  constructor({ userDataDir, safeStorage }) {
    if (!userDataDir || typeof userDataDir !== "string") {
      throw new Error("SecureStore 需要 userDataDir");
    }
    if (!safeStorage || typeof safeStorage.isEncryptionAvailable !== "function") {
      throw new Error("SecureStore 需要 Electron safeStorage");
    }
    this.userDataDir = userDataDir;
    this.safeStorage = safeStorage;
    this._clientKey = null;
  }

  init() {
    fs.mkdirSync(this.userDataDir, { recursive: true });
  }

  get encryptionAvailable() {
    return this.safeStorage.isEncryptionAvailable();
  }

  /** 读取或创建 Client 密钥对（RSA-2048）；私钥经 safeStorage 加密后落盘。 */
  async getOrCreateClientKey() {
    if (this._clientKey) return this._clientKey;
    const existing = await this._readClientKey();
    if (existing) {
      this._clientKey = existing;
      return existing;
    }
    if (!this.encryptionAvailable) {
      throw new Error("系统加密不可用，拒绝创建 Client 密钥（避免明文落盘）");
    }
    const { publicKey, privateKey } = await new Promise((resolve, reject) => {
      crypto.generateKeyPair("rsa", { modulusLength: 2048 }, (err, pub, priv) =>
        err ? reject(err) : resolve({ publicKey: pub.export({ type: "spki", format: "pem" }), privateKey: priv.export({ type: "pkcs8", format: "pem" }) }),
      );
    });
    await this._writeClientKey({ publicKeyPem: publicKey, privateKeyPem: privateKey });
    this._clientKey = { publicKeyPem: publicKey, privateKeyPem: privateKey };
    return this._clientKey;
  }

  /** 销毁内存中的密钥句柄；调用方在流程结束后主动清除。 */
  clearMemory() {
    this._clientKey = null;
  }

  async _clientKeyPath() {
    await this.init();
    return path.join(this.userDataDir, CLIENT_KEY_FILE);
  }

  async _readClientKey() {
    if (!this.encryptionAvailable) return null;
    const file = await this._clientKeyPath();
    let encrypted;
    try {
      encrypted = await fs.promises.readFile(file);
    } catch (err) {
      if (err && err.code === "ENOENT") return null;
      throw err;
    }
    const json = JSON.parse(this.safeStorage.decryptString(encrypted));
    if (!json || typeof json.publicKeyPem !== "string" || typeof json.privateKeyPem !== "string") {
      throw new Error("Client Key 文件格式损坏");
    }
    return { publicKeyPem: json.publicKeyPem, privateKeyPem: json.privateKeyPem };
  }

  async _writeClientKey(key) {
    if (!this.encryptionAvailable) {
      throw new Error("系统加密不可用，拒绝写盘 Client 密钥（避免明文落盘）");
    }
    const payload = JSON.stringify({ publicKeyPem: key.publicKeyPem, privateKeyPem: key.privateKeyPem });
    const encrypted = this.safeStorage.encryptString(payload);
    const file = await this._clientKeyPath();
    await fs.promises.writeFile(file, encrypted, { mode: 0o600 });
    try {
      fs.chmodSync(file, 0o600);
    } catch {
      /* Windows 上 chmod 忽略 */
    }
  }
}

module.exports = { SecureStore, CLIENT_KEY_FILE };
