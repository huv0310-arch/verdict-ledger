#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读复算链头四字段：entry_count / root_hash / issued_at / key_id。

用法：
  python examples/chain_head.py
  python examples/chain_head.py --journal <流水.jsonl>
  python examples/chain_head.py --journal <流水.jsonl> --expect <链头.json>

口径与 V2/v2_gate.py 的 chain-head 同一份实现（链上哈希直接复用 v2_gate，不另写公式）：
  entry_count  流水里非空行的条数
  root_hash    从首行起重算的链上最后一个哈希，带 sha256: 前缀
  issued_at    本次复算的时刻（Unix 秒，小数三位）
  key_id       锚文件最后一行的密钥指纹；读不到锚文件就是 null

--expect 模式拿发布的链头与流水复算结果比对：
  entry_count 与 root_hash 必须一字不差；
  issued_at 必须是数值且不晚于当前时刻；
  key_id 必须是 null 或 hmac-sha256: 加 16 位十六进制。
只读：不写任何文件、不联网、不读签名密钥（验签名要密钥，这条命令不碰密钥）。
输出永远是一行 JSON：对得上退出码 0，对不上或读不出来退出码 1。
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_GATE_DIR = os.path.join(ROOT, "V2")
if _GATE_DIR not in sys.path:
    sys.path.insert(0, _GATE_DIR)

import v2_gate as G          # 复用同一份链上哈希实现，两端才对得上

KEY_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{16}$")


def read_rows(path):
    """读流水的非空行（只读）。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def key_id_of(journal):
    """锚文件最后一行的 key_id：只读文件，不验签、不读密钥。"""
    anchor = os.environ.get("V2_ANCHORS") or os.path.join(
        os.path.dirname(os.path.abspath(journal)), "anchors.jsonl")
    try:
        last = None
        with open(anchor, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
    except OSError:
        return None
    if isinstance(last, dict) and isinstance(last.get("key_id"), str) and last["key_id"]:
        return last["key_id"]
    return None


def check_head(want, got):
    """发布链头与复算结果的四字段比对，返回问题清单（空 = 对得上）。"""
    problems = []
    try:
        want_count = int(want.get("entry_count"))
    except (TypeError, ValueError):
        want_count = None
    if want_count != got["entry_count"]:
        problems.append("entry_count 不符：发布 %r vs 复算 %d"
                        % (want.get("entry_count"), got["entry_count"]))
    if str(want.get("root_hash", "")) != got["root_hash"]:
        problems.append("root_hash 不符：发布 %r vs 复算 %s"
                        % (want.get("root_hash"), got["root_hash"]))
    ts = want.get("issued_at")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0 or ts > time.time() + 1:
        problems.append("issued_at 不是不晚于当前时刻的数值：%r" % (ts,))
    kid = want.get("key_id", "__missing__")
    if kid is not None and not (isinstance(kid, str) and KEY_ID_RE.match(kid)):
        problems.append("key_id 必须是 null 或 hmac-sha256: 加 16 位十六进制：%r" % (kid,))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="只读复算链头四字段（entry_count / root_hash / issued_at / key_id）")
    ap.add_argument("--journal", default=None,
                    help="流水文件路径（默认 V2 账目录下的 journal.jsonl）")
    ap.add_argument("--expect", default=None,
                    help="发布的链头 JSON 路径：与流水复算结果比对")
    args = ap.parse_args(argv)

    journal = args.journal or G.JOURNAL
    out = {"ok": False, "algo": G.ALGO, "entry_count": 0, "root_hash": "",
           "issued_at": None, "key_id": None, "why": ""}
    try:
        rows = read_rows(journal)
        n, head = G._head_of(rows)
        got = {"entry_count": n, "root_hash": ("sha256:" + head) if head else ""}
        out.update(got)
        out["issued_at"] = round(time.time(), 3)
        out["key_id"] = key_id_of(journal)
        if args.expect is None:
            out["ok"] = True
            out["why"] = "只读复算完成（未比对发布值）"
        else:
            with open(args.expect, encoding="utf-8") as f:
                want = json.loads(f.read())
            problems = check_head(want, got)
            if problems:
                out["recomputed"] = got
                out["why"] = "；".join(problems)
            else:
                out.update(entry_count=want["entry_count"],
                           root_hash=want["root_hash"],
                           issued_at=want["issued_at"],
                           key_id=want["key_id"], ok=True,
                           why="链头与流水一致（复算 %d 行）" % n)
    except Exception as exc:
        out["why"] = str(exc)[:200]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
