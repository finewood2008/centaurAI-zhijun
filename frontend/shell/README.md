# 知君独立桌面宿主

当前已实现 M0-L：独立 Vue 页面、安全 IPC、连接状态机、资料分页/筛选、取消与退出。页面由 `zhijun://desktop/desktop.html` 加载，不启动 Python，也不依赖 8618 服务。真实 SDK 业务身份桥尚未接通，默认只显示正式配置未就绪。

在仓库根安装依赖并启动：

```sh
rtk proxy npm --prefix frontend/mindos-web ci
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --simulation
```

`--simulation` 显式启用合成账号、两个盒子和47条资料，页面持续标注模拟环境。运行 `rtk proxy bash start-desktop.sh` 则进入默认未配置状态，登录返回配置未就绪。启动器每次先构建独立页面；已构建后可在本目录执行 `rtk proxy npm start` 或 `rtk proxy npm run start:simulation`。

开发宿主固定 Electron 37.10.3，已验证 macOS ARM64；尚未交付签名安装包或其他平台运行验收。打包环境禁止通过环境变量开启 simulation。旧 `ZHIJUN_BASE_URL` 不再使用，不能用它连接本机业务服务。

验证（前端依赖已安装，且先构建桌面页面）：

```sh
rtk proxy npm --prefix frontend/mindos-web run build:desktop
rtk proxy npm --prefix frontend/shell test
rtk proxy npm --prefix frontend/mindos-web run test:desktop
rtk proxy npm --prefix frontend/shell run test:e2e
```

E2E 启动真实 Electron，使用新建临时 userData 与合成数据，关闭后清理。无需后端、SDK连接、账号凭据或用户资料。`ZHIJUN_DESKTOP_USER_DATA` 是主进程测试隔离路径；日常开发默认使用独立的 `zhijun-desktop` 应用配置目录。模拟身份/资料只在内存中，renderer 使用非持久 session 分区。

实施、测试证据及正式接入待办见 [M0实施记录](../../docs/development/M0-IMPLEMENTATION-0906.md)。旧 `frontend/main.js` 与 `renderer/` 保留为历史代码，不在当前启动链。
