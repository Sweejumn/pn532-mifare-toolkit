#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PN532 直连驱动（不依赖 libnfc，按 Elechouse / Adafruit 官方帧格式实现）

为什么需要它：
  * libnfc 1.7.1 不导出 nfc_initiator_mifare_cmd，无法做单块写入
  * nfc-mfclassic 的 w 模式跳过块 0，W 模式只支持 gen1a 后门
  * 本驱动可对任意块写入（含块 0），适配 gen2/CUID 卡

三个关键实现细节（踩坑记录）：
  1. MIFARE 认证帧必须携带 UID：`60/61 <block> <key6> <uid4>`
     （Elechouse PN532.cpp / Adafruit PN532.py 都是这样实现的）
  2. 写块成功时该固件只返回 `D5 41 00`，没有 MIFARE 的 0x0A ACK
  3. 认证失败会让卡片进入 HALT 状态，后续操作必须重新选卡

命令行用法:
    python pn532.py --port COM13 probe
    python pn532.py --port COM13 read  <block> <key12> <A|B>
    python pn532.py --port COM13 write <block> <data32> <key12> <A|B>
    python pn532.py --port COM13 send  <hexbytes...>      # 原始命令诊断
"""
from __future__ import annotations

import os
import sys
import time

import serial

DEFAULT_PORT = os.environ.get("M1T_PORT", "COM13")
BAUD = 115200


class PN532:
    """PN532 UART 直连驱动（ISO14443-A / MIFARE Classic）"""

    def __init__(self, port: str = DEFAULT_PORT):
        self.uid = b""
        self.sak = 0
        self.sp = serial.Serial(port, BAUD, timeout=0.2, write_timeout=1)
        time.sleep(0.1)
        self.sp.write(bytes([0x55, 0x55, 0x00, 0x00, 0x00]))   # HSU 唤醒
        time.sleep(0.3)
        self.sp.reset_input_buffer()

    def close(self) -> None:
        try:
            self.sp.close()
        except Exception:
            pass

    # ---------- 帧层 ----------
    @staticmethod
    def build(payload: list[int]) -> bytes:
        ln = len(payload)
        lcs = (0x100 - ln) & 0xFF
        dcs = (0x100 - (sum(payload) & 0xFF)) & 0xFF
        return bytes([0x00, 0x00, 0xFF, ln, lcs]) + bytes(payload) + bytes([dcs, 0x00])

    def _read_raw(self, timeout: float) -> bytes:
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            n = self.sp.in_waiting
            if n:
                buf += self.sp.read(n)
                idx = buf.find(b"\x00\x00\xff")
                if idx >= 0 and len(buf) >= idx + 5:
                    ln = buf[idx + 3]
                    need = idx + 5 + (0 if ln == 0 else ln + 2)
                    if len(buf) >= need:
                        return buf
            else:
                time.sleep(0.01)
        return buf

    def cmd(self, payload: list[int], timeout: float = 1.0) -> tuple[bytes, bytes]:
        """发送命令，返回 (原始字节, 应答 payload)"""
        self.sp.reset_input_buffer()
        self.sp.write(self.build(payload))
        raw = self._read_raw(timeout)
        while True:                      # 跳过 ACK 帧
            idx = raw.find(b"\x00\x00\xff")
            if idx < 0:
                return raw, b""
            ln = raw[idx + 3] if len(raw) > idx + 3 else 0
            if ln == 0 and len(raw) >= idx + 6:
                raw = raw[idx + 6:]
                continue
            if len(raw) >= idx + 5 + ln + 2:
                return raw, raw[idx + 5: idx + 5 + ln]
            return raw, b""

    # ---------- 高层 ----------
    def sam(self) -> bool:
        _, r = self.cmd([0xD4, 0x14, 0x01, 0x14, 0x01], 0.8)
        return len(r) >= 2 and r[0] == 0xD5 and r[1] == 0x15

    def select(self) -> bytes | None:
        """选卡并解析 UID/SAK（认证帧需要 UID）"""
        _, r = self.cmd([0xD4, 0x4A, 0x01, 0x00], 1.2)
        if len(r) >= 7 and r[0] == 0xD5 and r[1] == 0x4B:
            try:
                n = r[7]
                self.uid = bytes(r[8: 8 + n])
                self.sak = r[6]
            except Exception:
                self.uid = b""
            return r
        return None

    def auth(self, block: int, key: bytes, use_a: bool = True):
        """MIFARE 认证（帧尾必须带 UID）"""
        cmd = ([0xD4, 0x40, 0x01, 0x60 if use_a else 0x61, block]
               + list(key) + list(self.uid))
        raw, r = self.cmd(cmd, 1.0)
        ok = len(r) >= 3 and r[0] == 0xD5 and r[1] == 0x41 and r[2] == 0x00
        return ok, raw, r

    def read_block(self, block: int) -> bytes | None:
        _, r = self.cmd([0xD4, 0x40, 0x01, 0x30, block], 1.0)
        if len(r) >= 19 and r[0] == 0xD5 and r[1] == 0x41 and r[2] == 0x00:
            return bytes(r[3:19])
        return None

    def write_block(self, block: int, data: bytes) -> bool:
        cmd = [0xD4, 0x40, 0x01, 0xA0, block] + list(data)
        _, r = self.cmd(cmd, 1.5)
        return len(r) >= 3 and r[0] == 0xD5 and r[1] == 0x41 and r[2] == 0x00

    # ---------- 便捷封装 ----------
    def read_card(self, keys: dict[int, tuple[str, str]], reselect_on_fail: bool = True) -> tuple[bytearray, set[int]]:
        """读整卡，返回 (1024 字节, 成功读取的扇区集合)"""
        got = bytearray(1024)
        ok_sectors: set[int] = set()
        for sec in range(16):
            ka, kb = keys.get(sec, ("FFFFFFFFFFFF", "FFFFFFFFFFFF"))
            authed = False
            for khex, use_a in ((ka, True), (kb, False), ("FFFFFFFFFFFF", True)):
                if not khex:
                    continue
                if self.auth(sec * 4, bytes.fromhex(khex), use_a)[0]:
                    authed = True
                    break
                if reselect_on_fail:
                    self.select()
            if not authed:
                continue
            good = True
            for b in range(4):
                blk = sec * 4 + b
                d = self.read_block(blk)
                if d is None:
                    good = False
                    break
                got[blk * 16:(blk + 1) * 16] = d
            if good:
                ok_sectors.add(sec)
        return got, ok_sectors


def _hex(s: str) -> bytes:
    s = s.replace(" ", "").replace("-", "")
    return bytes.fromhex(s)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = argv[1:]
    port = DEFAULT_PORT
    if "--port" in args:
        i = args.index("--port")
        port = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2

    mode = args[0]
    pn = PN532(port)
    try:
        if mode == "send":
            payload = [0xD4] + [int(x, 16) for x in args[1:] if x]
            raw, r = pn.cmd(payload, 1.2)
            print("TX:", PN532.build(payload).hex(" ").upper())
            print("RX raw:", raw.hex(" ").upper())
            print("RX payload:", r.hex(" ").upper())
            return 0

        if not pn.sam():
            print("SAMConfiguration 失败")
            return 1
        if not pn.select():
            print("未检测到卡片")
            return 3

        if mode == "probe":
            print(f"卡在位  UID={pn.uid.hex().upper()}  SAK={pn.sak:02X}")
            return 0

        block = int(args[1])
        key = _hex(args[3] if mode == "write" else args[2])
        use_a = (args[4] if mode == "write" else args[3]).upper() == "A"

        if mode == "read":
            ok, raw, r = pn.auth(block, key, use_a)
            if not ok:
                print(f"认证失败 payload={r.hex(' ').upper()}")
                return 4
            d = pn.read_block(block)
            if d is None:
                print("读块失败")
                return 5
            print(f"块{block}: {d.hex().upper()}")
            return 0

        if mode == "write":
            data = _hex(args[2])
            ok, raw, r = pn.auth(block, key, use_a)
            if not ok:
                print(f"认证失败 payload={r.hex(' ').upper()}")
                return 4
            if not pn.write_block(block, data):
                print("写块失败")
                return 5
            print(f"✅ 块{block} = {data.hex().upper()}")
            return 0

        print(__doc__)
        return 2
    finally:
        pn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
