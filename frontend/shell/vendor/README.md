# 固定 SDK 开发输入

以下归档来自相邻 SDK 仓库 `819831c143aa2ea269b979eac2297124054436f6` 的 `release/electron-sdk-1.2.0/`，复制前逐一校验 SHA-256。shell 通过本目录文件依赖和 package-lock 的完整性值安装，不依赖开发机绝对路径或浮动 npm 版本；仅主进程加载。

| 文件 | SHA-256 |
| --- | --- |
| nexusaos-connectivity-electron-1.2.0.tgz | `0637f4c3cbc309bc96e3be553b47b31e8a36279e1d6230fa7a0831a69b84bcea` |
| nexusaos-connectivity-contracts-1.0.1.tgz | `2a553d79061c8947fa234f42b69e905ebc6c9e964cae66fc0f4f2935c677cf41` |

使用了实际 SDK auth、main facade、admin ticket provider 和 process/native 协议，见 production 模块及其测试。sidecar 二进制不在本目录：当前来源 manifest 尚含旧 dirty 输入，未将其提升为正式发布产物。SDK 私有管道测试使用临时合成进程，只证明客户端装配/协议，不证明 Direct 网络或正式盒子授权。
