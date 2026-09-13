# Windows x64 打包与客户交付

## 支持范围与验收边界

本流程在 **Windows x64 + Node.js 22** 上构建 NSIS `.exe` 安装器和 ZIP；不支持直接在
Mac 上运行 Windows 一键构建，不支持当前流程生成 ARM64/32 位包。客户无需安装 Node.js。
安装后的使用需要有效账号、绑定盒子以及盒端服务可用。

默认命令要求 Windows 代码签名。`--unsigned` 是显式内部测试模式，独立文件名和输出目录，
不能作为已签名客户包交付。签名能确认发布者和完整性，但不保证新版本立即没有 SmartScreen 提示。

自动验包包括源码/依赖版本、前端资源、配置、PE 架构、原生程序哈希和 Authenticode；启动冒烟包括
搬移目录后启动、生产配置加载、隔离设置、DPAPI/safeStorage 往返及 sidecar 启动。
**不自动安装/卸载 NSIS，不访问真实账号/盒子，不宣称 P2P 或业务真机验收完成。**

## 首次准备

1. Windows 11 x64 构建机，安装 Git 和 Node.js 22 x64；建议使用专用构建机/干净虚拟机及
   当前用户私有的 NTFS 工作目录，避免共享可写目录。使用有桌面会话的环境运行 Electron 测试。
2. 获取包含本次改动的完整源码，确保 `frontend/shell/vendor/*.tgz` 都在；不要复制 Mac 的 `node_modules`。
3. 准备可信的 Windows x64 原生连接程序。当前固定源文件 SHA256：
   `e86f840d671f24c3a0fb3b892dc20ddbc227fac1a6d3c3252298c330e47a8705`。
   本地已有：`data/desktop/native-direct-budget-20260913/windows-amd64/nexusaos-connectivity-sidecar.exe`。
   `data/` 不入库，因此换构建机必须从可信构建产物单独传输，不能只克隆仓库就假设它存在。
   源程序应是未签名的原始产物；流水线校验固定哈希后再签名，签名后的新哈希写入配置。
4. 正式发布另需 Windows SDK 的 SignTool，以及当前用户证书存储 `CurrentUser\My` 中可签名的
   Authenticode 证书及私钥/硬件密钥。Apple Developer ID 证书不能替代 Windows 证书。
   不要将私钥/PFX/证书密码放入仓库或聊天。

在仓库根目录执行 PowerShell：

```powershell
node --version
npm --prefix frontend/mindos-web ci
npm --prefix frontend/shell ci
```

应显示 Node `v22.x`。依赖安装需要能访问 npm/Electron 下载服务；不要关闭 TLS 校验绕过下载问题。
Windows 构建调用 `test:windows` 显式跨平台测试集合，并执行 Electron 端到端测试。
历史依赖 chmod0600、POSIX shebang、`/usr/bin/openssl` 或需要管理员创建符号链接的测试仍保留在
原 Mac/Linux 全量测试中，不伪装为 Windows 已通过；真实 Windows DACL 测试在 Windows 上必须执行。

## 内部测试包（无签名证书）

```powershell
# 如果原生程序不在默认 data 路径，指定可信文件的绝对路径。
$env:ZHIJUN_SIDECAR_SOURCE = 'C:\build-inputs\nexusaos-connectivity-sidecar.exe'
npm --prefix frontend/shell run package:win-x64:unsigned
```

产物在 `frontend/shell/release-windows-unsigned/`：

- `Zhijun-UNSIGNED-<version>-win-x64.exe`
- `Zhijun-UNSIGNED-<version>-win-x64.zip`
- `verification-windows.json`、`smoke-windows.json`

未签名包可能被系统安全策略阻止。仅用于受控测试，不要求客户关闭系统安全功能。

## 签名客户包

```powershell
$env:ZHIJUN_SIDECAR_SOURCE = 'C:\build-inputs\nexusaos-connectivity-sidecar.exe'
# 替换为本机 Windows SDK 中真实的 signtool.exe 路径。
$env:ZHIJUN_WINDOWS_SIGNTOOL_PATH = 'C:\Program Files (x86)\Windows Kits\10\bin\<SDK版本>\x64\signtool.exe'
# 替换为当前用户 My 存储中的代码签名证书指纹：40位十六进制，不是密码。
$env:ZHIJUN_WINDOWS_CERT_SHA1 = '<40位证书指纹>'
npm --prefix frontend/shell run package:win-x64
```

签名使用 SHA256、RFC3161 时间戳。校验同时检查签名信任、预期发布者证书指纹和时间戳，
未配置签名或签名失败时正式流程直接报错，不静默降级成未签名包。
构建需要访问时间戳和证书校验相关服务。

产物在 `frontend/shell/release-windows/`：

- `Zhijun-<version>-win-x64.exe`：给客户安装。
- `Zhijun-<version>-win-x64.zip`：完整应用压缩包，不是安装器。
- `verification-windows.json`：哈希、签名及内容检查结果。
- `smoke-windows.json`：本机启动检查结果；`liveBoxTested`、`installerInstallTested` 仍为 false。

Windows 流程不自动增加版本，使用 `frontend/shell/package.json` 中版本，避免 Mac/Windows
各自打包导致版本漂移。正式发布前协调统一版本号；当前 Mac 命令仍保留原有自动 patch 行为。
同版本重跑时，旧 EXE/ZIP 和验包报告会保留到输出目录的 `previous-build-*` 子目录，避免把旧报告当作本次成功。

## 分阶段重跑

在依赖/前端构建/预加载构建已完成的情况下：

```powershell
npm --prefix frontend/shell run package:prepare:win-x64
npm --prefix frontend/shell run package:verify:win-x64
npm --prefix frontend/shell run package:smoke:win-x64
```

内部测试模式在命令后追加 `-- --unsigned`。验包需保留同一次构建的工作区、
`package-resources-windows/` 和产物；验包会拒绝源码、依赖锁或暂存配置与包不一致。
`prepare` 只准备资源，不生成安装器；不要在打包后重新 prepare 再验旧包，因为重新签名会改变哈希。

## 发客户前的人工验收

在没有开发环境、没有旧知君数据的 Windows 测试机上：

1. 从交付渠道下载 EXE，核对 SHA256 和数字签名。
2. 使用普通用户安装、启动，验证中文/带空格安装路径；不以管理员身份作为日常运行前提。
3. 登录、选择盒子、局域网直连、外网 Direct/TURN、上传、流式对话。
4. 如面向客户启用蓝牙配网或麦克风，单独验证真实硬件及授权流程。
5. 覆盖升级安装、卸载；卸载默认保留用户数据，确认没有误删资料或重置账号。

Windows 运行时使用 Windows DACL 安全检查，Mac/Linux 继续使用 POSIX 权限位；不能通过
全局放开文件权限来修复启动失败。生成文件和签名凭据已在 `.gitignore` 中排除。

## Mac 包交付说明

现有 0.1.17 Mac ARM64 包已 Developer ID 签名，但发布配置为 `notarize: false`，实际 Gatekeeper
检查为 `Unnotarized Developer ID`。它可以用于配合测试，但不保证客户直接下载打开。
正式发客户前应完成 Apple 公证、装订票据及干净机器下载验收；Intel Mac 需要另出对应架构。

参考：[Apple Developer ID](https://developer.apple.com/developer-id/)、
[SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)、
[SmartScreen 发布者说明](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation)。
