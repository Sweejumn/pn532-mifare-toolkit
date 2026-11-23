# 第三方组件说明（NOTICE）

本仓库作者编写的部分（`hardnested.py`、`.bat` 脚本、文档）采用 **MIT** 许可。

**Release 附件里的 zip 包**为了方便同学开箱即用，额外打包了下列第三方二进制。
它们不在此仓库源码树中（`.gitignore` 已排除 `nfc-bin/`），各自沿用上游许可：

| 文件 | 上游项目 | 许可 |
|---|---|---|
| `libnfc.dll` | [nfc-tools/libnfc](https://github.com/nfc-tools/libnfc) | LGPL-3.0 |
| `libusb0.dll` | [libusb](https://libusb.info/) (Win32 后端) | LGPL-2.1 |
| `nfc-list.exe` | [nfc-tools/libnfc](https://github.com/nfc-tools/libnfc) | LGPL-3.0 |
| `nfc-mfclassic.exe` | [nfc-tools/libnfc](https://github.com/nfc-tools/libnfc) | LGPL-3.0 |
| `mfoc.exe` | [nfc-tools/mfoc](https://github.com/nfc-tools/mfoc) | GPL-2.0-or-later |
| `libnfc_hardnested.exe` | 第三方编译版（作者署名 XAS-712），源码基于 nfc-tools 的 hardnested 实现 | 上游 GPL 系许可，请以原作者声明为准 |

说明：

1. 这些二进制由 **原发布方** 编译分发，本仓库只做调用，未修改其源码。
2. 如果你要**再分发**本 Release 的 zip（例如放到自己的镜像），请自行确认上述许可要求
   （GPL 组件通常要求随附源码或提供源码获取方式，LGPL 组件要求允许替换库文件）。
3. 想完全规避许可问题，可以不用 Release 里的二进制，改用你本地已有的
   `nfc-bin` 目录（把路径写进 `config.json` 的 `nfc_bin` 字段即可），
   或用上游源码自行编译后放进 `nfc-bin/`。
4. LGPL 的 `libnfc.dll` 与可执行文件是分离部署的（DLL 可替换），符合 LGPL 的动态链接要求。

本工具仅用于**自有卡片或已获授权的测试环境**。
