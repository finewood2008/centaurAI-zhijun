"use strict";
/**
 * 后端进程托管 + stdio 点对点 RPC 客户端（Stage 2）。
 *
 * 职责：
 * - 以子进程方式拉起 Python 后端（`server.py --stdio-rpc`），随 Electron 生命周期启停；
 * - 与后端子进程经 stdin/stdout 交换 JSON-RPC 帧，按 id 关联请求/响应；
 * - 提供就绪门控：后端真正可用（HTTP 8618 探活成功，即 lifespan 已完成）前，
 *   所有 RPC 调用排队等待，避免消息跑到 watcher/线程池/Chroma 就绪之前；
 * - 兜底降级：若本进程拉起的子进程无法获得实例锁而退出，但外部已有后端在 8618
 *   提供服务，则自动回退为 HTTP 调用（保持原有行为）。
 *
 * 协议帧（与 backend/rpc_stdio.py 保持一致，一行一个 UTF-8 JSON）：
 *   请求  {id, method, uri, headers?, body?, form?, file?{name,filename,type,base64}}
 *   响应  {id, status, headers?, body, bodyBase64?} 或 {id, error:{message}}
 */
const { spawn } = require("child_process");
const path = require("path");
const readline = require("readline");

const API_BASE = "http://127.0.0.1:8618";
// 与后端 preload 一致的 CSRF 头（仅 HTTP 兜底路径需要；stdio 路径直接复用 ASGI 中间件校验）
const CSRF_HEADERS = { "X-Requested-By": "centaur-vdb" };

class BackendRpc {
  constructor() {
    this.child = null;
    this.nextId = 1;
    this.pending = new Map(); // id -> {resolve, reject, timer}
    this.ready = null; // Promise；解析后表示后端可服务请求
    this._started = false;
    this.logHistory = [];
  }

  resolveDir() {
    return path.join(__dirname, "..", "backend");
  }

  _resolvePython() {
    const backend = this.resolveDir();
    const win = process.platform === "win32";
    const exe = win
      ? path.join(backend, ".venv", "Scripts", "python.exe")
      : path.join(backend, ".venv", "bin", "python");
    const plain = win ? "python.exe" : "python3";
    const plainPath = win ? path.join(backend, plain) : plain;
    return { exe, plain: plainPath };
  }

  _log(line) {
    if (!line) return;
    // 保留最近 200 行，供错误诊断
    this.logHistory.push(line);
    if (this.logHistory.length > 200) this.logHistory.splice(0, this.logHistory.length - 200);
  }

  start() {
    if (this._started) return this.ready;
    this._started = true;

    const backend = this.resolveDir();
    let { exe, plain } = this._resolvePython();
    // 优先 venv 解释器；缺失则退回系统 python（依赖需已安装到该解释器）
    let python = exe;
    try {
      require("fs").accessSync(exe);
    } catch {
      python = plain;
    }

    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });

    // 探活：8618 /api/health 可用 => lifespan 已完成(watcher/锁/Chroma 就绪)
    this._pollReady(30000).then(
      () => {
        this._resolveReady && this._resolveReady(true);
      },
      (err) => {
        this._rejectReady && this._rejectReady(err);
      },
    );

    const spawnErr = (err) => {
      this._log(`[backend] spawn error: ${err && err.message}`);
      this._rejectReady && this._rejectReady(err);
    };

    this.child = spawn(python, [path.join(backend, "server.py"), "--stdio-rpc"], {
      cwd: backend,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.child.on("error", spawnErr);
    this.child.stderr.on("data", (d) => this._handleStderr(d));
    const rl = readline.createInterface({ input: this.child.stdout });
    rl.on("line", (line) => this._handleLine(line));
    this.child.on("exit", (code, signal) => {
      this._log(`[backend] exited code=${code} signal=${signal}`);
      this._flushError(new Error(`后端进程已退出(code=${code}))`));
    });

    return this.ready;
  }

  _handleStderr(data) {
    const text = data.toString();
    this._log(`[backend] ${text.trimEnd()}`);
  }

  _handleLine(line) {
    // 严格按帧解析；非 JSON 的赶路输出直接忽略（stdout 专用于协议流）
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      this._log(`[backend] (ignored non-JSON stdout) ${line.slice(0, 120)}`);
      return;
    }
    if (msg && typeof msg.id !== "undefined" && msg.id !== null) {
      const holder = this.pending.get(msg.id);
      if (holder) {
        this.pending.delete(msg.id);
        clearTimeout(holder.timer);
        holder.resolve(msg);
      }
      return;
    }
    this._log(`[backend] (unrouted frame) ${line.slice(0, 200)}`);
  }

  _pollReady(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    const attempt = () => {
      if (Date.now() > deadline) {
        const last = [this.logHistory.slice(-8), `外部 8618 探活超时`].flat();
        return Promise.reject(new Error("后端启动超时：" + last.join("\n")));
      }
      return fetch(`${API_BASE}/api/health`)
        .then((r) => (r.ok ? true : Promise.reject(new Error(`health ${r.status}`))))
        .catch(() => new Promise((res) => setTimeout(() => res(attempt()), 1000)));
    };
    return attempt();
  }

  /** 统一入口：await 就绪后底层派发。req 允许下一次调用直接传已含 id 的帧。 */
  async rpc(req) {
    if (!this.ready) this.start();
    await this.ready.catch(() => {
      /* 就绪失败由具体传输在失败时抛错 */
    });
    const frame = {
      method: req.method || "GET",
      uri: req.uri,
      headers: req.headers,
      body: req.body,
      form: req.form,
      file: req.file,
    };
    if (this.child && this.child.exitCode === null && !this.child.killed) {
      // 主路径：经 stdio 点对点
      return this._stdio(frame);
    }
    // 兜底：子进程已退，序列化降级为 HTTP(8618)（外部后端仍在线时）
    return this._httpFallback(frame);
  }

  _stdio(frame) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`RPC 超时（id=${id}, ${frame.method} ${frame.uri}）`));
      }, 600000);
      this.pending.set(id, { resolve, reject, timer });
      const payload = JSON.stringify({ ...frame, id });
      try {
        this.child.stdin.write(payload + "\n");
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  _flushError(err) {
    for (const [, holder] of this.pending) {
      clearTimeout(holder.timer);
      holder.reject(err);
    }
    this.pending.clear();
  }

  // ---- HTTP 兜底：等价于旧 preload.js 的 fetch 逻辑 ----
  async _httpFallback(frame) {
    // 二进制请求体（file）按 multipart 重放
    if (frame.file && frame.file.base64) {
      const fd = new FormData();
      const buf = Buffer.from(frame.file.base64, "base64");
      fd.append(frame.file.name || "file", new Blob([buf], { type: frame.file.type || "application/octet-stream" }), frame.file.filename || "upload.bin");
      for (const [k, v] of Object.entries(frame.form || {})) fd.append(k, String(v));
      return this._httpToFrame(`${API_BASE}${frame.uri}`, {
        method: frame.method,
        headers: { ...CSRF_HEADERS },
        body: fd,
      });
    }
    const init = { method: frame.method || "GET", headers: {} };
    if (frame.headers) Object.assign(init.headers, frame.headers);
    if (frame.method && frame.method !== "GET" && !(frame.headers && frame.headers["content-type"])) {
      init.headers["content-type"] = "application/json";
      init.headers["X-Requested-By"] = "centaur-vdb";
    }
    if (frame.body != null) init.body = frame.body;
    return this._httpToFrame(`${API_BASE}${frame.uri}`, init);
  }

  async _httpToFrame(url, init) {
    const resp = await fetch(url, init);
    const ctype = resp.headers.get("content-type") || "";
    const isBinary = !(ctype.startsWith("text/") || ctype.includes("json") || ctype === "");
    const body = isBinary ? Buffer.from(await resp.arrayBuffer()).toString("base64") : await resp.text();
    return {
      id: null,
      status: resp.status,
      headers: { "content-type": ctype },
      body,
      bodyBase64: isBinary,
    };
  }

  get baseUrl() {
    return API_BASE;
  }

  async stop() {
    const child = this.child;
    if (!child) return;
    try {
      child.stdin.end();
    } catch {}
    // 给子进程几秒正常收尾（Chroma 干净关闭）；超时强杀
    child.kill();
    setTimeout(() => {
      if (child && child.exitCode === null) child.kill("SIGKILL");
    }, 5000).unref();
  }
}

module.exports = { BackendRpc, API_BASE };