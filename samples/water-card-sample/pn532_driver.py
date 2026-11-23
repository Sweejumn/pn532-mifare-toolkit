"""PN532 底层驱动（按 libnfc 的帧格式实现）—— 用于诊断与单块读写

用法:
  python tools/pn532.py probe
  python tools/pn532.py read  <block> <key12> <A|B>
  python tools/pn532.py write <block> <data32> <key12> <A|B>
  python tools/pn532.py send  <hexbytes...>        # 直接发任意命令，打印原始应答
"""
from __future__ import annotations

import os
import sys
import time

import serial
from pathlib import Path

PORT = os.environ.get("M1T_PORT", "COM13")
BAUD = 115200


class PN532:
    def __init__(self, port: str = PORT):
        self.uid = b""
        self.sak = 0
        self.sp = serial.Serial(port, BAUD, timeout=0.2, write_timeout=1)
        time.sleep(0.1)
        self.sp.write(bytes([0x55, 0x55, 0x00, 0x00, 0x00]))   # 唤醒
        time.sleep(0.3)
        self.sp.reset_input_buffer()

    def close(self):
        try:
            self.sp.close()
        except Exception:
            pass

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
                # 至少拿到一个完整帧再看
                if len(buf) >= 8 and b"\x00\x00\xff" in buf:
                    # 判断是否已有 DCS+POSTAMBLE
                    idx = buf.find(b"\x00\x00\xff")
                    if len(buf) >= idx + 5:
                        ln = buf[idx + 3]
                        if ln == 0 and len(buf) >= idx + 6:
                            return buf
                        if len(buf) >= idx + 5 + ln + 2:
                            return buf
            else:
                time.sleep(0.01)
        return buf

    def cmd(self, payload: list[int], timeout: float = 1.0) -> tuple[bytes, bytes]:
        """发送命令，返回 (原始字节, 解析出的应答 payload)"""
        self.sp.reset_input_buffer()
        raw_tx = self.build(payload)
        self.sp.write(raw_tx)
        raw = self._read_raw(timeout)
        # 跳过 ACK
        while True:
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

    # ---- 高层操作 ----
    def sam(self) -> bool:
        _, r = self.cmd([0xD4, 0x14, 0x01, 0x14, 0x01], 0.8)
        return len(r) >= 2 and r[0] == 0xD5 and r[1] == 0x15

    def select(self) -> bytes | None:
        _, r = self.cmd([0xD4, 0x4A, 0x01, 0x00], 1.2)
        if len(r) >= 7 and r[0] == 0xD5 and r[1] == 0x4B:
            # D5 4B NbTg Tg SENS_RES(2) SEL_RES(1) NFCIDLength(1) NFCID(n)
            try:
                ln_idx = 7
                n = r[ln_idx]
                self.uid = bytes(r[ln_idx + 1: ln_idx + 1 + n])
                self.sak = r[6]
            except Exception:
                self.uid = b""
            return r
        return None

    def auth(self, block: int, key: bytes, use_a: bool = True):
        # Elechouse 实现：InDataExchange + 1 + auth_cmd + block + key6 + UID
        cmd = [0xD4, 0x40, 0x01, 0x60 if use_a else 0x61, block] + list(key) + list(self.uid)
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
        # 该 PN532 固件只返回 D5 41 00（无 0x0A 字节）
        return len(r) >= 3 and r[0] == 0xD5 and r[1] == 0x41 and r[2] == 0x00



# ---------------- 分区重试写入（追加到 pn532.py 的 main 之前） ----------------
def reselect(pn: "PN532") -> bool:
    """认证失败会让卡进入 HALT，必须重新选卡。"""
    return pn.select() is not None


def write_card_sectorwise(pn: "PN532", dump: bytes, sec_keys: dict[int, tuple[str, str]],
                          rounds: int = 12, verbose: bool = True) -> list[int]:
    """按扇区写入。sec_keys: {扇区: (KeyA, KeyB)}，用 A 为主，失败再用 B（中间重新选卡）。"""
    done: set[int] = set()
    for rnd in range(1, rounds + 1):
        if len(done) == 16:
            break
        if verbose:
            print(f"--- 第 {rnd} 轮：已完成 {len(done)}/16 扇区 ---")
        for sec in range(16):
            if sec in done:
                continue
            ka, kb = sec_keys.get(sec, ("FFFFFFFFFFFF", "FFFFFFFFFFFF"))
            authed = False
            for khex, use_a in ((ka, True), (kb, False)):
                ok, _, _ = pn.auth(sec * 4, bytes.fromhex(khex), use_a)
                if ok:
                    authed = True
                    break
                reselect(pn)          # 失败后卡被 HALT，必须重新选卡
            if not authed:
                continue
            sec_ok = True
            for b in range(4):
                blk = sec * 4 + b
                if not pn.write_block(blk, dump[blk*16:(blk+1)*16]):
                    sec_ok = False
                    break
            if sec_ok:
                done.add(sec)
                if verbose:
                    print(f"    扇区{sec:02d} ✅")
            else:
                reselect(pn)
        time.sleep(0.2)
    return sorted(set(range(16)) - done)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode = argv[1]
    pn = PN532()
    try:
        if mode == "send":
            payload = [0xD4] + [int(x, 16) for x in argv[2:] if x]
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
            print("✅ 卡片在位")
            return 0

        if mode == "writecard":
            import json
            dump = Path(argv[2]).read_bytes()
            kf = Path(argv[3]) if len(argv) > 3 else None
            sec_keys = {}
            if kf and kf.exists():
                kj = json.loads(kf.read_text("utf-8-sig"))
                for sname, row in kj.items():
                    try:
                        si = int(sname)
                    except ValueError:
                        continue
                    if isinstance(row, list):
                        sec_keys[si] = (row[0] or "FFFFFFFFFFFF", row[1] or "FFFFFFFFFFFF")
                    elif isinstance(row, dict):
                        sec_keys[si] = (row.get("A") or "FFFFFFFFFFFF", row.get("B") or "FFFFFFFFFFFF")
            else:
                ff = argv[3] if len(argv) > 3 else "FFFFFFFFFFFF"
                for si in range(16):
                    sec_keys[si] = (ff, ff)
            bad = write_card_sectorwise(pn, dump, sec_keys)
            print(f"\n完成：{16-len(bad)}/16 扇区  失败：{bad}")
            return 0 if not bad else 1

        block = int(argv[2])
        if mode == "read":
            key = bytes.fromhex(argv[3])
            use_a = argv[4].upper() == "A"
            ok, raw, r = pn.auth(block, key, use_a)
            print("auth raw:", raw.hex(" ").upper())
            print("auth payload:", r.hex(" ").upper())
            if not ok:
                print("认证失败")
                return 4
            d = pn.read_block(block)
            if d is None:
                print("读块失败")
                return 5
            print(f"块{block}: {d.hex().upper()}")
            return 0

        if mode == "write":
            data = bytes.fromhex(argv[3])
            key = bytes.fromhex(argv[4])
            use_a = argv[5].upper() == "A"
            ok, raw, r = pn.auth(block, key, use_a)
            print("auth payload:", r.hex(" ").upper())
            if not ok:
                print("认证失败")
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

