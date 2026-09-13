# 固定 SDK 开发输入

以下当前生产归档来自相邻 SDK 仓库 `819831c143aa2ea269b979eac2297124054436f6`，复制前逐一校验 SHA-256。shell 通过本目录文件依赖和 package-lock 的完整性值安装，不依赖开发机绝对路径或浮动 npm 版本；仅主进程加载。

| 文件 | SHA-256 |
| --- | --- |
| nexusaos-connectivity-electron-1.3.0.tgz | `b1dd71f122232402ed0a28e73ad757823946be52f68a89e68aa6c1d4ee2bf284` |
| nexusaos-connectivity-contracts-1.1.0.tgz | `fd83a0612237d161a8531dbf04f5dbe6e3a71fc030993e027208935a746950b6` |

保留以下旧归档仅用于显式回滚；生产 `package.json` 不引用它们：

| 文件 | SHA-256 |
| --- | --- |
| nexusaos-connectivity-electron-1.2.0.tgz | `0637f4c3cbc309bc96e3be553b47b31e8a36279e1d6230fa7a0831a69b84bcea` |
| nexusaos-connectivity-contracts-1.0.1.tgz | `2a553d79061c8947fa234f42b69e905ebc6c9e964cae66fc0f4f2935c677cf41` |

使用了实际 SDK auth、main facade、admin ticket provider 和 process/native 协议，见 production 模块及其测试。sidecar 二进制不在本目录；macOS arm64 生产输入固定为 `data/desktop/native-1.3.0/sidecar/darwin-arm64/nexusaos-connectivity-sidecar`，SHA-256 为 `ffe277893e2b02eb2442dd35d5a9ce68fa582f8880ffe06fc417a2c39d42fcb5`。SDK 私有管道测试使用临时合成进程，只证明客户端装配/协议，不证明 Direct 网络或正式盒子授权。
