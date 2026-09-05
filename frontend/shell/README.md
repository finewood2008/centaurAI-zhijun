# 知君桌面薄壳

只做一件事：打开一个窗口加载本机后端提供的 `http://127.0.0.1:8618/mindos/`。没有 preload、没有 IPC 桥、不关闭 webSecurity；后端没起来时显示等待页并每 3 秒重试。

```bash
cd frontend/shell && npm install
npm start                        # 需要先 ./start-backend.sh
ZHIJUN_BASE_URL=http://127.0.0.1:8618 npm start
npm run smoke                    # 加载成功或显示等待页后自动退出，用于验证
```

旧的 `frontend/main.js` + `frontend/renderer/` 是上一代「个人记忆库」的 Electron 应用，与知君无关，计划退役。
