# tools/ — 底层卡片工具

基于 **PN532 直连驱动**，不依赖 libnfc，可以做到 libnfc 工具做不到的事（写块 0、单块读写、按扇区写入）。

## 为什么需要这些工具

| 需求 | libnfc / nfc-mfclassic | 本目录工具 |
|---|---|---|
| 单块读写 | ❌ 只能整卡 | ✅ |
| 写块 0（改 UID） | ⚠️ `w` 跳过、`W` 仅 gen1a | ✅ 认证后普通写（gen2 适用） |
| 弱信号下写入 | ❌ 整卡写，中间断即全废 | ✅ 按扇区写，失败只重试该扇区 |
| 认证失败恢复 | ❌ 不处理 HALT | ✅ 自动重新选卡 |

## 文件

| 文件 | 说明 |
|---|---|
| `pn532.py` | PN532 UART 直连驱动（认证带 UID、CRC 由芯片处理） |
| `card_sync.py` | 读卡 / 按扇区写卡 / 只改次数（跨卡同步用） |

## 用法

```powershell
# 1. 测卡是否在位
python tools/pn532.py --port COM13 probe

# 2. 读单块（诊断）
python tools/pn532.py --port COM13 read 0 F3DD1B9B0343 A

# 3. 读整卡
python tools/card_sync.py read --port COM13 --keys known_keys.json --out card.mfd

# 4. 按扇区写整卡（弱信号下可跑多轮）
python tools/card_sync.py write --port COM13 --keys known_keys.json --dump card.mfd

# 5. 只改次数（保持余额/金额不变，同步扇区 2/3）
python tools/card_sync.py setcount --port COM13 --keys known_keys.json --count 78

# 6. 只接受实体卡（SAK=0x88），避免误写克隆卡
python tools/card_sync.py write --only-sak 88 --dump card.mfd
```

## 密钥文件格式

与 `known_keys.json` 兼容：

```json
{
  "0": ["F3DD1B9B0343", "F3DD1B9B0343"],
  "7": {"A": "73796E707562", "B": "356508EA85C3"}
}
```

## 三个关键实现细节（踩坑记录）

1. **MIFARE 认证帧必须带 UID**
   ```
   60/61 <block> <key6> <uid4>
   ```
   [Elechouse PN532.cpp](https://github.com/elechouse/PN532) 的
   `mifareclassic_AuthenticateBlock` 和 [Adafruit PN532.py](https://github.com/adafruit/Adafruit_Python_PN532)
   都是这样实现的。**少了 UID，PN532 直接返回 `7F`（无效帧）。**

2. **写块成功的判断**
   部分 PN532 固件只返回 `D5 41 00`，没有 MIFARE 的 `0x0A` ACK。
   判定条件应为 `D5 41 00`，不要强求 `0x0A`。

3. **认证失败会让卡进入 HALT**
   libnfc 源码注释明确："When a MIFARE Classic AUTH fails, the tag is automatically in HALT state"。
   每次认证失败后必须重新 `InListPassiveTarget` 选卡，否则后续全部失败。

## 关于 MIFARE 尾块的显示

读尾块时 **Key A 字段恒为 `000000000000`**，这是 MIFARE 规范（Key A 永不可读），
不代表密钥写错了。要验证 Key A 是否正确，用认证测试（能认证就是对的）。

## 兼容性

- 读卡器：PN532（UART / HSU 模式，CH340 或 CP2102 转接）
- 卡：MIFARE Classic 1K；块 0 可写需 gen2 / CUID 卡
- 依赖：Python 3.9+、pyserial
