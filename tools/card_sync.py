#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡片读写工具集（基于 pn532.py 直连驱动）

用法:
    # 读整卡并保存为 .mfd
    python card_sync.py read  --port COM13 --keys known_keys.json --out card.mfd

    # 按扇区写整卡（弱信号下可反复推进）
    python card_sync.py write --port COM13 --keys known_keys.json --dump card.mfd

    # 只改余额/次数（扇区2/3 块0），保持其它字段
    python card_sync.py setcount --port COM13 --keys known_keys.json --count 78

    # 只接受某种卡（防止误写）：--only-sak 88 表示只接受 SAK=0x88 的实体卡
    python card_sync.py write --port COM13 --keys known_keys.json --dump card.mfd --only-sak 88

密钥文件格式（与 known_keys.json 兼容）:
    {"0": ["F3DD1B9B0343", "F3DD1B9B0343"], "1": {"A": "...", "B": "..."}, ...}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pn532 import PN532  # noqa: E402


def load_keys(path: Path) -> dict[int, tuple[str, str]]:
    raw = json.loads(path.read_text("utf-8-sig"))
    out: dict[int, tuple[str, str]] = {}
    for k, v in raw.items():
        try:
            si = int(k)
        except ValueError:
            continue
        if isinstance(v, dict):
            out[si] = (v.get("A") or "FFFFFFFFFFFF", v.get("B") or "FFFFFFFFFFFF")
        elif isinstance(v, list):
            out[si] = (v[0] or "FFFFFFFFFFFF", v[1] or "FFFFFFFFFFFF")
    return out


def xor_of(b: bytes) -> int:
    x = 0
    for v in b[:15]:
        x ^= v
    return x


def describe(block: bytes) -> str:
    bal = block[0] | (block[1] << 8)
    amt = (block[9] << 8) | block[8]
    return (f"余额 {bal/100:.2f} 元 · 次数 {block[12]} · 金额 {amt/100:.2f} · "
            f"XOR {'✅' if xor_of(block) == block[15] else '❌'}")


def wait_card(pn: PN532, limit: int, only_sak: int | None, keys: dict) -> bytes | None:
    """轮询等待卡片，返回块0；only_sak 用于限定卡类型"""
    t0 = time.time()
    warned = False
    while time.time() - t0 < limit:
        if pn.select() is None:
            time.sleep(0.15)
            continue
        b0 = None
        for khex in ("F3DD1B9B0343", "FFFFFFFFFFFF"):
            ok, _, _ = pn.auth(0, bytes.fromhex(khex), True)
            if ok:
                b0 = pn.read_block(0)
                break
            pn.select()
        if b0 is None:
            continue
        if only_sak is not None and b0[5] != only_sak:
            if not warned:
                print(f"  当前卡 SAK={b0[5]:02X}，不是要求的 {only_sak:02X}，继续等待…")
                warned = True
            time.sleep(0.3)
            continue
        return b0
    return None


def cmd_read(args, keys) -> int:
    pn = PN532(args.port)
    try:
        if not pn.sam():
            print("SAM 失败")
            return 1
        b0 = wait_card(pn, args.wait, args.only_sak, keys)
        if b0 is None:
            print("等待超时")
            return 1
        print(f"✅ 读到卡  UID={pn.uid.hex().upper()}  SAK={pn.sak:02X}")
        got, secs = pn.read_card(keys)
        out = Path(args.out)
        out.write_bytes(bytes(got))
        print(f"读到扇区 {len(secs)}/16  →  {out}")
        print(f"块0: {bytes(got[0:16]).hex().upper()}")
        print(describe(bytes(got[128:144])))
        return 0
    finally:
        pn.close()


def cmd_write(args, keys) -> int:
    src = Path(args.dump).read_bytes()
    if len(src) != 1024:
        print("dump 必须是 1024 字节")
        return 2
    pn = PN532(args.port)
    try:
        if not pn.sam():
            print("SAM 失败")
            return 1
        b0 = wait_card(pn, args.wait, args.only_sak, keys)
        if b0 is None:
            print("等待超时")
            return 1
        print(f"✅ 卡在位  UID={pn.uid.hex().upper()}  SAK={pn.sak:02X}")

        # 若要求保留卡片自身块0，用它覆盖 dump 的块0
        target = bytearray(src)
        if args.keep_block0:
            target[0:16] = b0
            print("  保留卡片自身块 0")

        done: set[int] = set()
        for rnd in range(1, args.rounds + 1):
            if len(done) == 16:
                break
            print(f"--- 第 {rnd} 轮：{len(done)}/16 ---")
            for sec in range(16):
                if sec in done:
                    continue
                ka, kb = keys.get(sec, ("FFFFFFFFFFFF", "FFFFFFFFFFFF"))
                authed = False
                for khex, use_a in ((ka, True), (kb, False), ("FFFFFFFFFFFF", True)):
                    if not khex:
                        continue
                    if pn.auth(sec * 4, bytes.fromhex(khex), use_a)[0]:
                        authed = True
                        break
                    pn.select()
                if not authed:
                    continue
                ok = True
                for b in range(4):
                    blk = sec * 4 + b
                    if blk == 0 and args.skip_block0:
                        continue
                    if not pn.write_block(blk, bytes(target[blk * 16:(blk + 1) * 16])):
                        ok = False
                        break
                if ok:
                    done.add(sec)
                    print(f"  扇区{sec:02d} ✅")
                else:
                    pn.select()
            time.sleep(0.2)
        print(f"完成 {len(done)}/16 扇区")
        return 0 if len(done) == 16 else 1
    finally:
        pn.close()


def cmd_setcount(args, keys) -> int:
    """只改次数（保持余额与其它字段），同步到扇区 2 和 3"""
    pn = PN532(args.port)
    try:
        if not pn.sam():
            print("SAM 失败")
            return 1
        b0 = wait_card(pn, args.wait, args.only_sak, keys)
        if b0 is None:
            print("等待超时")
            return 1
        print(f"✅ 卡在位  UID={pn.uid.hex().upper()}  SAK={pn.sak:02X}")

        cur = None
        for khex in ("485473521AC0", "FFFFFFFFFFFF"):
            pn.select()
            if pn.auth(8, bytes.fromhex(khex), True)[0]:
                cur = pn.read_block(8)
                break
        if cur is None:
            print("读扇区2失败")
            return 3
        print(f"  当前: {describe(cur)}")
        if cur[12] == args.count:
            print("  次数已是目标值")
            return 0

        new = bytearray(cur)
        new[12] = args.count
        new[15] = 0
        new[15] = xor_of(bytes(new))
        print(f"  目标: {new.hex().upper()}")
        for blk in (8, 12):
            pn.select()
            if not pn.auth(blk, bytes.fromhex("485473521AC0"), True)[0]:
                print(f"  块{blk} 认证失败")
                continue
            print(f"  块{blk}: {'✅' if pn.write_block(blk, bytes(new)) else '❌'}")

        pn.select(); pn.auth(8, bytes.fromhex("485473521AC0"), True)
        v = pn.read_block(8)
        if v:
            print(f"  回读: {describe(v)}")
            return 0 if v == bytes(new) else 1
        return 1
    finally:
        pn.close()


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="PN532 卡片读写工具")
    ap.add_argument("--port", default="COM13")
    ap.add_argument("--keys", default="known_keys.json")
    ap.add_argument("--wait", type=int, default=300, help="等待卡片秒数")
    ap.add_argument("--only-sak", type=lambda x: int(x, 16), default=None,
                    help="只接受该 SAK 的卡（如 88 = 实体卡）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read", help="读整卡")
    p.add_argument("--out", default="card.mfd")

    p = sub.add_parser("write", help="按扇区写整卡")
    p.add_argument("--dump", required=True)
    p.add_argument("--rounds", type=int, default=12)
    p.add_argument("--skip-block0", action="store_true", help="不写块0（手机虚拟卡）")
    p.add_argument("--keep-block0", action="store_true", help="用卡片自身块0 覆盖 dump")

    p = sub.add_parser("setcount", help="只改次数")
    p.add_argument("--count", type=int, required=True)

    args = ap.parse_args(argv[1:])
    keys = load_keys(Path(args.keys)) if Path(args.keys).exists() else {}
    if args.cmd == "read":
        return cmd_read(args, keys)
    if args.cmd == "write":
        return cmd_write(args, keys)
    if args.cmd == "setcount":
        return cmd_setcount(args, keys)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
