# 知君 Windows 打包操作指南

更新日期：2026-09-14

本文面向 Windows 打包操作人员。先生成内部测试包，完成安装及盒子连接验证，再准备签名客户包。

> 本流程只生成 Windows 桌面客户端，不升级盒端 worker、Data Engine 或 Remote Agent。

## 1. 准备构建电脑

- Windows 11 x64 电脑。
- Git。
- Node.js **22 x64**，包含 npm。
- 能访问项目 Git 仓库、npm 和 Electron 下载服务。
- 使用正常登录的桌面会话执行，不在没有桌面的后台服务中运行 Electron 测试。
- 使用当前用户私有的本地 NTFS 工作目录，不使用公共共享可写目录。

当前脚本仅支持 Windows x64，不支持此流程生成 Windows ARM64 或 32 位安装包。

打开 PowerShell，检查环境：

```powershell
git --version
node --version
node -p "process.platform + ' ' + process.arch"
npm.cmd --version
```

Node.js 应显示 `v22.x`，平台和架构应显示 `win32 x64`。版本或架构不符时，先安装正确版本，再重新打开 PowerShell。

## 2. 获取完整源码

使用有权限的账号克隆知君项目，并切换到负责人指定的发布分支或提交。不要凭分支名称猜测哪个版本可发布。

以下命令中的路径是示例；后续操作均在项目根目录执行：

```powershell
cd C:\work\nexusaos-centuarai-zhijun
git status --short
git rev-parse HEAD
```

项目中至少应包含：

```text
frontend\mindos-web\package.json
frontend\shell\package.json
frontend\shell\package-lock.json
frontend\shell\vendor\
frontend\shell\scripts\build-windows-package.cjs
```

`frontend\shell\vendor\` 中的 `.tgz` 依赖必须齐全。不要复制 Mac 上的 `node_modules`、Electron 程序或打包暂存目录到 Windows。

## 3. 单独准备 Windows 连接程序

客户端需要 Windows x64 原生连接程序：

```text
nexusaos-connectivity-sidecar.exe
```

当前可信源文件在 Mac 项目目录下：

```text
data/desktop/native-direct-budget-20260913/windows-amd64/nexusaos-connectivity-sidecar.exe
```

> `data/` 被 Git 忽略，因此仅克隆项目不会取得这个文件。请让项目负责人通过可信渠道单独提供，不要从不明来源下载。

将原始、未签名的源文件复制到 Windows，例如：

```text
C:\build-inputs\nexusaos-connectivity-sidecar.exe
```

检查 SHA256：

```powershell
Get-FileHash 'C:\build-inputs\nexusaos-connectivity-sidecar.exe' -Algorithm SHA256
```

本文对应版本的预期 SHA256 为：

```text
e86f840d671f24c3a0fb3b892dc20ddbc227fac1a6d3c3252298c330e47a8705
```

大小写差异不影响比较。如果不一致，停止打包并联系负责人。不要修改脚本中的预期哈希来绕过检查；后续发布若更新原生程序，应同步审核源码中的固定哈希。

## 4. 安装项目依赖

在项目根目录执行：

```powershell
npm.cmd --prefix frontend/mindos-web ci
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败，请先处理错误' }

npm.cmd --prefix frontend/shell ci
if ($LASTEXITCODE -ne 0) { throw '桌面宿主依赖安装失败，请先处理错误' }
```

两步都成功后再继续。使用 `npm.cmd` 可以避免 PowerShell 选择 `npm.ps1` 时遇到执行策略限制，无需为此关闭系统安全策略。

## 5. 生成内部测试包

没有 Windows 签名证书时，先运行明确的未签名测试流程：

```powershell
$env:ZHIJUN_SIDECAR_SOURCE = 'C:\build-inputs\nexusaos-connectivity-sidecar.exe'

npm.cmd --prefix frontend/shell run package:win-x64:unsigned
if ($LASTEXITCODE -ne 0) { throw 'Windows 打包未完成，请保留错误日志' }
```

脚本依次执行 Windows 测试、桌面 UI 测试、Electron 端到端测试、前端构建、资源准备、EXE/ZIP 打包、验包和启动冒烟检查。中间失败会停止，不应跳过失败步骤后直接交付产物。

### 产物位置

```text
frontend\shell\release-windows-unsigned\
```

主要文件：

- `Zhijun-UNSIGNED-<版本>-win-x64.exe`：内部测试安装器。
- `Zhijun-UNSIGNED-<版本>-win-x64.zip`：完整应用压缩包，不是安装器。
- `verification-windows.json`：内容及哈希检查报告。
- `smoke-windows.json`：启动检查报告。

Windows 打包流程不自动增加版本号，版本取自 `frontend\shell\package.json`。发布前由负责人统一版本；同版本重跑时，旧产物和报告会保存到输出目录的 `previous-build-*` 子目录。

> 未签名包仅用于受控内部测试，不作为签名客户包交付。若系统安全策略阻止运行，不要求客户关闭安全功能，应走组织批准的测试流程或使用签名包。

## 6. 安装与实际业务验收

自动验包和启动检查不等于完整产品验收。它们不会自动执行 NSIS 安装/卸载，也不会登录真实账号、连接真实盒子。

建议在干净测试机或干净虚拟机中完成以下检查：

- [ ] 使用普通用户安装 EXE 并启动，不以管理员身份作为日常运行前提。
- [ ] 验证中文或带空格的安装路径。
- [ ] 登录知君账号并选择已绑定盒子。
- [ ] 在局域网连接盒子，打开对话、本体、资料页面。
- [ ] 验证本地模型和云端模型的流式对话，等待正常完成。
- [ ] 上传文件，确认上传及处理状态。
- [ ] 验证检索材料确认、脱敏选择和授权行为。
- [ ] 换到外网后验证连接，并记录实际 Direct/TURN 路径。
- [ ] 如需交付蓝牙配网或麦克风功能，使用真实硬件验证权限及功能。
- [ ] 验证升级安装、卸载与用户数据保留行为。

客户安装已完成的安装包不需要 Node.js 或 Git，但正常使用仍需要有效账号、已绑定盒子和可用的盒端服务。

## 7. 生成正式签名客户包

正式流程还需要：

1. Windows SDK 提供的 `signtool.exe`。
2. 当前用户证书存储 `CurrentUser\My` 中可用的代码签名证书及私钥或硬件密钥。
3. 能访问时间戳和证书校验相关服务。

Apple Developer ID 证书不能代替 Windows 代码签名证书。不要将私钥、PFX 文件或证书密码放入项目仓库、聊天或普通日志。

在 PowerShell 中配置实际路径和证书指纹：

```powershell
$env:ZHIJUN_SIDECAR_SOURCE = 'C:\build-inputs\nexusaos-connectivity-sidecar.exe'

# 将 <SDK版本> 替换为这台电脑实际安装的 SDK 版本。
$env:ZHIJUN_WINDOWS_SIGNTOOL_PATH = 'C:\Program Files (x86)\Windows Kits\10\bin\<SDK版本>\x64\signtool.exe'

# 替换为代码签名证书的 40 位十六进制指纹，不是证书密码。
$env:ZHIJUN_WINDOWS_CERT_SHA1 = '<40位证书指纹>'

npm.cmd --prefix frontend/shell run package:win-x64
if ($LASTEXITCODE -ne 0) { throw '正式签名包生成或验包失败，不能交付' }
```

正式产物位于：

```text
frontend\shell\release-windows\
```

交付安装器为 `Zhijun-<版本>-win-x64.exe`。签名缺失或校验失败时，正式流程会报错，不会静默降级为未签名包。签名并不保证新版本立即没有 SmartScreen 提示，仍需完成下载、安装及业务验收。

可记录交付 EXE 的哈希：

```powershell
# 将 <版本> 替换为本次产物的真实版本。
Get-FileHash '.\frontend\shell\release-windows\Zhijun-<版本>-win-x64.exe' -Algorithm SHA256
```

## 8. 常见问题与反馈信息

| 现象 | 处理方式 |
| --- | --- |
| Node 版本或架构检查失败 | 安装 Node.js 22 x64，重新打开 PowerShell 后检查。 |
| 缺少原生连接程序 | 单独取得可信的 Windows sidecar，并检查 `ZHIJUN_SIDECAR_SOURCE`。 |
| sidecar 哈希不一致 | 停止打包，确认源文件和发布版本，不绕过固定哈希检查。 |
| npm 或 Electron 下载失败 | 检查网络、代理及下载服务可达性，不关闭 TLS 校验。 |
| 正式流程提示缺少签名配置 | 内部测试使用 `package:win-x64:unsigned`；客户包必须准备证书及 SignTool。 |
| 测试、验包或启动检查失败 | 保留首次报错，处理后重跑；不要将残留 EXE 当作成功产物。 |
| 安装成功但无法连接盒子 | 分别检查账号、绑定关系、网络和盒端服务；客户端安装成功不代表盒端已升级。 |

遇到问题，请提供：

- Windows 版本与 CPU 架构。
- `node --version`、`npm.cmd --version` 输出。
- `git rev-parse HEAD` 输出。
- 执行的命令、首个错误及相关上下文。
- 本次生成的验包和启动报告（如有）。

发送日志前检查并移除密码、API Token、证书私钥等敏感信息。

## 9. 打包检查清单

- [ ] 源码来自指定发布分支或提交。
- [ ] Node.js 22 x64，平台为 Windows x64。
- [ ] `vendor/*.tgz` 齐全，未复制 Mac 的依赖目录。
- [ ] Windows sidecar 来源可信且哈希匹配。
- [ ] 两处 `npm ci` 成功。
- [ ] 一键打包所有步骤成功。
- [ ] 确认使用的是本次产物和本次验包报告。
- [ ] 安装器和实际盒子业务测试通过。
- [ ] 客户交付使用正式签名包，并记录版本、提交及安装器 SHA256。

维护者详细说明：[Windows x64 打包与客户交付](windows-release.md)。
