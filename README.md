# pn532-mifare-toolkit

PN532 读卡器 + MIFARE Classic 卡的**一键密钥破解 / 校验 / 读卡导出 / 写 CUID 卡**工具。
单文件 Python，零第三方依赖，中文界面输出。

> 仅用于**自有卡片**或已获授权的测试环境。

---

## 给同学：最快用法（不用装 Python 环境之外的任何东西）

1. 打开右侧 **[Releases](../../releases)**，下载最新的 `pn532-mifare-toolkit.zip`
2. 解压到任意目录（路径别带中文也行）
3. 装好 PN532 的 USB-TTL 驱动（CH340 / CP2102），插上读卡器
4. 把卡贴到读卡器上，**双击 `run.bat`**

看到下面这些就是正常：

```
✅ 读卡器在 COM8
✅ 读到卡片 UID=A1B2C3D4
字典扫描完成：命中 N 把密钥
扇区7 KeyB  计算  43.2%   已用  12分   剩余约  18分
✅ 扇区7 KeyB = 356508EA85C3 已保存
✅ 已导出 card.mfd（1024 字节）与 card.mct
```

zip 里已经带了 `nfc-bin/`（libnfc + nfc-list + nfc-mfclassic + libnfc_hardnested + mfoc），
**不需要另外装 libnfc**。

先自检：双击 `selftest.bat`，或 `python hardnested.py selftest`。

---

## 功能

| 入口 | 命令 | 作用 |
|---|---|---|
| `run.bat` | `python hardnested.py auto` | 自动找串口 → 等卡 → 字典扫描 → 只破缺的 → 导出 |
| `status.bat` | `python hardnested.py status` | 当前任务 / 百分比 / 密钥完成度 |
| `write.bat` | `python hardnested.py write` | 把 dump 写进空白 CUID 卡（带原卡检测+确认+备份） |
| `selftest.bat` | `python hardnested.py selftest` | 环境自检，排错第一步 |

其它命令：

```powershell
python hardnested.py auto --dry-run       # 只扫描，看缺哪些密钥（不耗时）
python hardnested.py crack 8A 7B          # 手动指定扇区+密钥类型
python hardnested.py verify               # 逐扇区真实认证校验
python hardnested.py dump                 # 读整卡导出 .mfd / .mct
python hardnested.py --port COM5 auto     # 手动指定串口
python hardnested.py --version
```

---

## 特性说明

**串口自动发现**：遍历系统 COM 口逐个探测，找到接读卡器的那个（换 USB 口自动适配，写回 `config.json`）。

**只破需要的**：先用 `mfoc` 做秒级字典扫描，命中的直接保存，剩下的才排进 HardNested 队列。

**断点续跑**：每解出一把密钥立刻写 `state/keys.json`；关机/崩溃后重启会自动校验并跳过已完成的。
代价是"当前正在算的那一个扇区"要从头重算（HardNested 的计算进度无法存档）。

**实时进度**：计算阶段在同一窗口原地刷新百分比、已用时间、预计剩余，同时逐行写入 `logs/*.log`。

**写卡三道防护**：

1. 先在读卡器上读当前卡 UID，与 dump 里的 UID 相同 → **直接拒绝**（防止把数据写回源卡）
2. 屏幕上显示目标卡 UID 与写入内容，必须手打 `确认写入` 四个字
3. 写前自动备份目标卡为 `out/backup_<UID>.mfd`

写完自动回读整卡与 dump 逐字节比对，0 差异才算成功。

---

## 从源码运行

```powershell
git clone https://github.com/Sweejumn/pn532-mifare-toolkit.git
cd pn532-mifare-toolkit
# 需要 nfc-bin：从 Release 的 zip 里拷出来，或用上游源码自行编译
python hardnested.py selftest
python hardnested.py auto
```

`config.json` 里的 `nfc_bin` 支持相对路径；若指向的目录不存在，会自动回退到包内 `nfc-bin/`。

---

## 底层工具（tools/）

除了一键脚本，仓库还带一套 **PN532 直连驱动**，用于 libnfc 做不到的操作：

| 工具 | 能力 |
|---|---|
| `tools/pn532.py` | 单块读写、写块 0、认证诊断 |
| `tools/card_sync.py` | 读整卡 / 按扇区写卡 / 只改次数（跨卡同步） |

详见 [tools/README.md](tools/README.md)。实测经验：认证帧必须携带 UID，否则 PN532 直接回 `7F`。

## 目录结构

```
m1t-hardnested/
├─ hardnested.py      主程序（单文件，零依赖）
├─ config.json        串口 / nfc-bin 路径 / 参考密钥 / 重试
├─ README.md          本文件
├─ AGENT-HANDOFF.md   交接文档（运行链路、断点语义、排错表、退出码）
├─ run.bat status.bat write.bat selftest.bat   四个入口
├─ nfc-bin/           随包二进制（仓库里被 .gitignore 排除，只在 Release 里提供）
├─ state/             运行时生成：keys.json / status.json / dump_meta.json
├─ logs/              运行时生成：<命令>-<时间戳>.log + latest.log
└─ out/               运行时生成：card.mfd / card.mct / backup_<UID>.mfd
```

---

## 常见问题

**报 `Invalid serial port` 或所有串口"无响应"**
libnfc 默认会自动扫描所有串口，遇到蓝牙虚拟串口（COM3/9/10/11…）会卡死几分钟。
本程序已自动设置 `LIBNFC_DEVICE=pn532_uart:<port>` 和 `LIBNFC_AUTO_SCAN=false`；
如果你手工调用 `nfc-bin` 里的二进制，也要带上这两个环境变量。

**设备管理器里 CH340 显示未插入 / 串口号变了**
换 USB 口会重新枚举（COM13 ↔ COM14 ↔ COM8）。程序会自动重新探测并写回 `config.json`，不用手改。

**报 `A device attached to the system is not functioning`**
USB-TTL 驱动/设备处于故障态：拔掉 USB、等 3 秒、换一个 USB 口。

**进度条停在 43% 就出结果**
正常。百分比是"状态空间搜索进度"，扫到真密钥就停，不是耗时比例。

**读整卡报 `块 0x.. 认证错误`**
只要有一个扇区密钥不对，`nfc-mfclassic` 就整体中止且不写文件。先 `selftest` 看密钥是否 32/32。

更多排错细节见 [AGENT-HANDOFF.md](AGENT-HANDOFF.md) 第 5 节（症状 → 原因 → 修复）。

---

## 许可

本仓库代码：**MIT**，见 [LICENSE](LICENSE)。
随 Release 分发的第三方二进制（libnfc / mfoc / nfc-mfclassic 等）沿用其上游许可，见 [NOTICE](NOTICE.md)。
