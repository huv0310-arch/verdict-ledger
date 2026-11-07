"""判决账本的记账闸（record 入口）。

- 每次调用记一笔：verdict（1 / 0）+ 判决种类 + 情形指纹，只增不改。
- 同 key 护栏：同值不重复入账；异值且非权威来源拦下不覆盖；
  纠偏走 [翻案] 追加 —— 历史一行不删，只盖章。
- fail-open：任何异常一律吞掉，调用方永不因账本坏掉而断。

沿革与设计记录见 git log。"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "v2_gate")
STATE_DIR = os.environ.get("V2_STATE_DIR") or os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), ".hermes", "state", "v2")  # 账目录可配
JOURNAL = os.path.join(STATE_DIR, "journal.jsonl")
SNAP = os.path.join(STATE_DIR, "ledger.json")
WRITER = os.environ.get("V2_WRITER", "v2-front-door")
__version__ = "3.1"             # 对外发布 tag v3.1（2026-11-07）；沿革见 git log / CHANGELOG（对外发布时另附）
TTL = 3600   # 旧世代（v1）统一窗，仅历史节自带此值，本文件不再写入

_Ledger = None
if SNAP_DIR not in sys.path:
    sys.path.insert(0, SNAP_DIR)
try:
    from ledger import Ledger as _Ledger  # 快照正本，不碰 ganglion 工作副本
except Exception:
    _Ledger = None


def _key(platform, msg):
    """设计决定（2026-11-04）：key 只哈希清洗后的内容，
    不带平台（#2 不看表头 / #6 key 是公转）、清洗与 ctx_fp 同一份（#4 同维度）、不含 ts（#7）。
    旧公式 sha256(f"{platform}::{msg}") 是「世代 v1」，只在查询侧双查时用（另行发布）。
    platform 参数保留只为兼容旧调用签名。"""
    try:
        from ctx_fp import key_of
        return key_of(msg)
    except Exception:
        import hashlib
        raw = f"{platform}::{msg or ''}".encode("utf-8", "ignore")
        return hashlib.sha256(raw).hexdigest()[:16]


def _window(key, L):
    """窗＝这个 key 上一圈到上上一圈的实测距离（2026-11-04 定：按下面几条结论算，不拍数字）：
    π —— 每位现场算，3600 和 120 都是查表；上一圈的经验定下一圈；
    时间是场 —— 窗是这节自己走出来的，不是外面盖的戳。
    第一圈没有圈距 → None＝「这节没有钟」：sweep_ttl 天然跳过它（没钟不杀）、probe 不借它，
    忘交给 prune（判过没人翻才忘）—— 不能拿肠子的节奏管神经元。"""
    try:
        chain = (getattr(L, "nodes", None) or {}).get(key) or []
        ts = sorted(n.get("ts") for n in chain if isinstance(n.get("ts"), (int, float)))
        if len(ts) < 2:
            return None
        return max(60.0, ts[-1] - ts[-2])     # 圈距即窗；同秒抖动兜底 60s
    except Exception:
        return None


GUARD_LOG = os.path.join(STATE_DIR, "guard_iterfix.jsonl")   # 护栏留痕（fail-open，只记动作不记全文）


def _guard_log(action, k, kind, old_v, new_v):
    """护栏留痕：谁被拦、谁走了翻案。key 只留头尾（红线：key 不回显）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        ks = k if isinstance(k, str) else ""
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "action": action,
                                "key": (ks[:4] + "…" + ks[-2:]) if len(ks) > 8 else "?",
                                "kind": str(kind)[:80], "old": old_v, "new": new_v},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def record(platform, msg, verdict, kind, ctx_fp="", cross_fp=None, flip=False):
    """一条判决入账。绝不抛异常、绝不阻塞。
    ctx_fp = 情形指纹（2026-11-04 接线：ledger.append 一直收这个参数，此前没人传）。
    flip = 权威改判标记（护栏）：同 key 已有生效判决且新值不同时，
      flip=False（快通道/MCP 这类启发式重判）→ 拦下不覆盖（第二次不覆盖，红线「同 key 必同值」）；
      flip=True （纠偏·自审这类人工改判）→ 走 [翻案] 戳追加（历史不删只盖章）。
      同值重复入账一律不追加（防刷行/防圈距灌水）。
      护栏内任何异常 → 走本函数既有 except → return False（不写、不抛，调用方照旧）。"""
    if _Ledger is None:
        return False
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(SNAP):
            L = _Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)  # 账本级不给钟
        else:
            L = _Ledger(writer=WRITER, ttl=None)   # 账本级不给钟（新节由 _window 现场算）
        _k = _key(platform, msg)
        _v = int(verdict)
        _chain = (getattr(L, "nodes", None) or {}).get(_k) or []
        _old = None
        for _n in reversed(_chain):               # 当前生效判决＝链里最新一条
            if _n.get("verdict") in (0, 1):
                _old = int(_n["verdict"])
                break
        if _old is not None and _old == _v:
            _guard_log("skip_same", _k, kind, _old, _v)          # 同值重复：不追加
            L.sweep_ttl()
            try:
                L.prune()
            except Exception:
                pass
            L.save(SNAP, journal=JOURNAL)
            _chain_sync()
            return True
        if _old is not None and _old != _v and not flip:
            _guard_log("skip_conflict", _k, kind, _old, _v)      # 冲突且非权威：拦下不覆盖
            L.sweep_ttl()
            try:
                L.prune()
            except Exception:
                pass
            L.save(SNAP, journal=JOURNAL)
            _chain_sync()
            return True
        reason = (f"[翻案] v={_v} round={len(_chain) + 1} old={_old} why={kind}"
                  if _old is not None else f"[判决] v={_v} {kind}")
        if _old is not None:
            _guard_log("overturn", _k, kind, _old, _v)           # 权威改判：带戳追加
        if cross_fp is None:            # 不传就自己按 key 算（各端同 key 必同值）
            try:
                import ctx_fp as _cf
                cross_fp = _cf.cross(_k)
            except Exception:
                cross_fp = ""
        L.append(JOURNAL, _k, _v, reason, ttl=_window(_k, L),   # 圈距即窗；第一圈=None=没钟
                 ctx_fp=ctx_fp, cross_fp=cross_fp)
        L.sweep_ttl()          # D-007：出窗的节归档后原地变墓碑（内部 fail-open，绝不抛）
        try:
            # 生长/分解：新节一、二圈没有钟（时间不杀），忘得由 prune 动刀——
            # 「判过、没人翻过、也没参与翻转的平淡节」才死（人不是长成的，是死成一个的）。
            L.prune()
        except Exception:
            pass
        L.save(SNAP, journal=JOURNAL)
        _chain_sync()          # 判决落账即上链（影子文件，journal 本体不动）
        return True
    except Exception:
        return False


_CHAIN = JOURNAL + ".chain"   # 影子链：与 journal 同目录，append-only


def _line_h(prev, obj):
    """一行一个哈希：sha256(上一行哈希 + 本行规范化 JSON)——链式，改中间断全链。"""
    import hashlib
    body = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + "|" + body).encode("utf-8")).hexdigest()


def _chain_sync():
    """给 journal 缺链的尾部行补链（幂等，只 append chain 尾）。
    中间已链行被改不会在这里被覆盖——留给 verify_chain() 抓，同步不帮篡改者销案。fail-open。"""
    try:
        if not os.path.exists(JOURNAL):
            return
        rows = []
        with open(JOURNAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        n_chained, last_h = 0, ""
        if os.path.exists(_CHAIN):
            with open(_CHAIN, encoding="utf-8") as f:
                cl = [json.loads(x) for x in f if x.strip()]
            n_chained, last_h = len(cl), (cl[-1].get("h", "") if cl else "")
        if n_chained >= len(rows):
            return
        prev = last_h
        with open(_CHAIN, "a", encoding="utf-8") as f:
            for n in range(n_chained, len(rows)):
                prev = _line_h(prev, rows[n])
                f.write(json.dumps({"n": n, "h": prev}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def verify_chain():
    """全量重算比对影子链。返回 {ok, lines, chained, break_at, why}。
    抓三种坏：①journal 某行被改（哈希不匹配）②chain 缺尾/被截断 ③journal 删行（chain 比 journal 长）。"""
    try:
        if not os.path.exists(JOURNAL):
            return {"ok": True, "lines": 0, "chained": 0, "break_at": None}
        rows = []
        with open(JOURNAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        chain = []
        if os.path.exists(_CHAIN):
            with open(_CHAIN, encoding="utf-8") as f:
                chain = [json.loads(x) for x in f if x.strip()]
        prev = ""
        for n, row in enumerate(rows):
            prev = _line_h(prev, row)
            if n >= len(chain):
                return {"ok": False, "lines": len(rows), "chained": len(chain),
                        "break_at": n, "why": "chain缺尾（新行未链或被截断）"}
            if chain[n].get("h") != prev:
                return {"ok": False, "lines": len(rows), "chained": len(chain),
                        "break_at": n, "why": "哈希不匹配：journal 第 %d 行事后被改？" % n}
        if len(chain) > len(rows):
            return {"ok": False, "lines": len(rows), "chained": len(chain),
                    "break_at": len(rows), "why": "chain 比 journal 长：有行被删？"}
        return {"ok": True, "lines": len(rows), "chained": len(chain), "break_at": None}
    except Exception as e:
        return {"ok": False, "lines": -1, "chained": -1, "break_at": None, "why": str(e)[:100]}


# ══════════ 链头锚定（v3.1）：给「链头」留一个能对账的凭据 ══════════
# 影子链防的是「改行 / 删行 / 单侧截断」—— 全量重算当场断。
# 防不住的是：把 journal 和 .chain **一起**从尾部截掉几行 —— 剩下的链条依然自洽。
# 锚定 = 把某一刻的链头（条数 + 摘要）追加写进一个**独立的锚文件**（append-only），
# 校验时拿本地链头去对锚记录：本地比锚旧 = 尾巴被截了。
# 边界照实说：锚与账同机同盘时，防的是截断与残缺；流水 + 链 + 锚一起改的共谋防不住，
# 外部第三方时间戳锚定（把链头发到链外）还没做 —— 账被当证据用的时候才需要那一层。
ANCHORS = os.environ.get("V2_ANCHORS") or os.path.join(STATE_DIR, "anchors.jsonl")
ALGO = "sha256-chain"


def _anchor_path(path=None):
    return path or ANCHORS


def _journal_rows():
    """读 journal 的非空行（只读）。"""
    rows = []
    if not os.path.exists(JOURNAL):
        return rows
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _head_of(rows, upto=None):
    """从首行起重算链上哈希：upto = 只算前 upto 行（默认整份）。返回 (条数, 十六进制)。"""
    prev, n = "", 0
    for row in (rows if upto is None else rows[:upto]):
        prev = _line_h(prev, row)
        n += 1
    return n, prev


def chain_head():
    """只读：当前链头摘要 —— 条数 + 链上最后一个哈希 + 时刻。不改账、不写盘。"""
    try:
        rows = _journal_rows()
        n, head = _head_of(rows)
        chain = []
        if os.path.exists(_CHAIN):
            with open(_CHAIN, encoding="utf-8") as f:
                chain = [json.loads(x) for x in f if x.strip()]
        res = {"ok": True, "algo": ALGO, "lines": n, "chained": len(chain),
               "head": ("sha256:" + head) if head else "", "ts": round(time.time(), 3), "why": ""}
        if head and chain and chain[-1].get("h") != head:
            res["ok"] = False
            res["why"] = "影子链最后一行与 journal 重算的链头对不上（先跑 verify 看断在哪）"
        return res
    except Exception as e:
        return {"ok": False, "algo": ALGO, "lines": -1, "chained": -1,
                "head": "", "why": str(e)[:120]}


def anchor(path=None):
    """把当前链头**追加**写进独立锚文件（append-only：{ts, count, head, algo}）。

    只写锚文件：journal 与 .chain 一个字节不改（这是只读接口的那半边）。
    链头读不出来 / 账 0 行就不写 —— 没得锚就不假装锚过。fail-open 同本文件其余接口。
    """
    ap = _anchor_path(path)
    try:
        h = chain_head()
        if not h.get("ok"):
            return {"ok": False, "path": ap, "why": h.get("why") or "链头读不出来"}
        if not h.get("lines"):
            return {"ok": False, "path": ap, "why": "账 0 行：没有可锚的链头"}
        rec = {"ts": h["ts"], "count": h["lines"], "head": h["head"], "algo": ALGO}
        d = os.path.dirname(ap)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(ap, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out = dict(rec)
        out.update({"ok": True, "path": ap, "why": "链头已追加进锚文件"})
        return out
    except Exception as e:
        return {"ok": False, "path": ap, "why": str(e)[:120]}


def verify_against(path=None):
    """拿本地链头对锚记录 —— 抓**尾部被截断**（journal + .chain 一起截也抓得到）。

    口径（三问，缺一不可）：
      · 锚文件不存在 / 是空的 → ok=False：没得比就是没验，绝不报绿；
      · 本地行数 < 最新锚的行数 → 尾部被截断（truncated=True）；
      · 本地行数 >= 锚行数 → 把本地流水算到**锚那一行的位置**，链上哈希必须等于锚记的
        head（对得上 = 锚点之前的账一个字没被动过；账在锚之后继续长是正常的）。
    锚只钉到锚那一条：锚之后的行靠更新的锚与影子链管。
    """
    ap = _anchor_path(path)
    try:
        if not os.path.exists(ap):
            return {"ok": False, "path": ap,
                    "why": "锚文件不存在：没有可比对的锚（先 anchor 记一笔）"}
        with open(ap, encoding="utf-8") as f:
            anchors = [json.loads(x) for x in f if x.strip()]
        if not anchors:
            return {"ok": False, "path": ap, "why": "锚文件是空的：没有可比对的锚"}
        a = anchors[-1]
        cnt = int(a.get("count") or 0)
        rows = _journal_rows()
        local = len(rows)
        base = {"path": ap, "lines": local, "anchor": a, "truncated": False}
        if local < cnt:
            base.update({"ok": False, "truncated": True,
                         "why": f"尾部被截断：本地 {local} 行 < 锚记录 {cnt} 行"
                                f"（锚于 {a.get('ts')}）"})
            return base
        n, head = _head_of(rows, cnt)
        head = "sha256:" + head if head else ""
        if head != a.get("head"):
            base.update({"ok": False, "local_head": head,
                         "why": f"链头与锚不符：本地算到第 {n} 行的链头对不上锚"
                                f"（锚那一段的账被动过？）"})
            return base
        tail = local - cnt
        base.update({"ok": True, "head": chain_head().get("head", ""),
                     "why": "链头与锚一致" + (f"（锚之后账继续长了 {tail} 行）" if tail else "")})
        return base
    except Exception as e:
        return {"ok": False, "path": ap, "why": str(e)[:120]}


def overturn(platform, msg, new_verdict, why, ctx_fp=""):
    """翻案：对同 key 追加一条反转判决——历史不删只盖章：旧行还在，新行 reason 带
    「[翻案] v=N round=R old=O why=...」，翻案信息全在 reason 里，ledger 一行不改。
    无原判→False；同值→False（同值不叫翻案）。fail-open 同 record，绝不抛。"""
    if _Ledger is None:
        return False
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(SNAP):
            L = _Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)
        else:
            L = _Ledger(writer=WRITER, ttl=None)
        _k = _key(platform, msg)
        chain = (getattr(L, "nodes", None) or {}).get(_k) or []
        old = None
        for n in reversed(chain):     # 当前生效判决＝链里最新一条（含历史翻案行）
            if n.get("verdict") in (0, 1):
                old = n
                break
        if old is None:
            return False              # 无原判，无案可翻
        old_v, new_v = int(old.get("verdict")), int(new_verdict)
        if old_v == new_v:
            return False              # 同值不叫翻案
        R = len(chain) + 1
        reason = f"[翻案] v={new_v} round={R} old={old_v} why={why}"
        cross_fp = ""
        try:
            import ctx_fp as _cf
            cross_fp = _cf.cross(_k)
        except Exception:
            pass
        L.append(JOURNAL, _k, new_v, reason, ttl=_window(_k, L),
                 ctx_fp=ctx_fp, cross_fp=cross_fp)
        L.save(SNAP, journal=JOURNAL)
        _chain_sync()          # 判决落账即上链（影子文件，journal 本体不动）
        return True
    except Exception:
        return False


PENDING = os.path.join(STATE_DIR, "overturn_pending.jsonl")


def _load_pend():
    rows = []
    try:
        with open(PENDING, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def _save_pend(rows):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PENDING + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, PENDING)


def flag_mistake(platform, msg, why_note, ctx_fp=""):
    """翻案触发端（预测误差亮悬位，V3 第一轮 2026-11-05）：
    事后证据说原判可能错了 → 给该 key 挂一笔 mismatch 悬位。
    不改判决、不占账格，只留「在走」的痕迹；确认错走 close_pending(False)→overturn()，
    确认对走 close_pending(True)→upheld。同 key 已有 open 悬位→tries+1 不重复开。
    fail-open 同 record。"""
    if _Ledger is None:
        return False
    try:
        _k = _key(platform, msg)
        rows = _load_pend()
        for r in rows:
            if r.get("key") == _k and r.get("status") == "open":
                r["tries"] = int(r.get("tries", 1)) + 1
                r["last_ts"] = time.time()
                r["need"] = why_note or r.get("need", "")
                _save_pend(rows)
                return True
        chain_len = 0
        if os.path.exists(SNAP):
            L = _Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)
            chain_len = len((getattr(L, "nodes", None) or {}).get(_k) or [])
        rows.append({"t": "pending", "key": _k, "platform": platform, "msg": msg,
                     "why": "mismatch", "need": why_note, "round": chain_len or 1,
                     "tries": 1, "ctx_fp": ctx_fp, "writer": WRITER,
                     "first_ts": time.time(), "last_ts": time.time(),
                     "status": "open", "close_ts": None, "close_note": ""})
        _save_pend(rows)
        return True
    except Exception:
        return False


def pendings(status="open"):
    """列悬位（巡检/对账机扫）。status=None 全量。"""
    try:
        rows = _load_pend()
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return {"ok": True, "count": len(rows), "pendings": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def close_pending(platform, msg, upheld, note=""):
    """收口悬位：upheld=True 原判站住→dismissed；False→判错，联动 overturn()。
    无 open 悬位→False。fail-open。"""
    if _Ledger is None:
        return False
    try:
        _k = _key(platform, msg)
        rows = _load_pend()
        hit = None
        for r in rows:
            if r.get("key") == _k and r.get("status") == "open":
                hit = r
        if hit is None:
            return False
        if upheld:
            hit["status"] = "upheld"
        else:
            if not overturn(platform, msg, 0 if _current_verdict(platform, msg) == 1 else 1,
                            note or "pi-verify"):
                return False          # 翻不动就不收口，悬位留着继续走
            hit["status"] = "overturned"
        hit["close_ts"] = time.time()
        hit["close_note"] = note
        _save_pend(rows)
        return True
    except Exception:
        return False


def _current_verdict(platform, msg):
    try:
        _k = _key(platform, msg)
        L = _Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)
        chain = (getattr(L, "nodes", None) or {}).get(_k) or []
        for n in reversed(chain):
            if n.get("verdict") in (0, 1):
                return int(n["verdict"])
    except Exception:
        pass
    return None


def stats():
    if _Ledger is None:
        return {"ok": False, "error": "snapshot ledger missing"}
    try:
        if not os.path.exists(SNAP):
            return {"ok": True, "keys": 0, "nodes": 0}
        L = _Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)  # 账本级不给钟
        nodes = [n for v in L.nodes.values() for n in v]
        over = 0
        try:   # 翻案数：reason 前缀 [翻案] 扫 journal（fail-open）
            with open(JOURNAL, encoding="utf-8", errors="ignore") as f:
                over = sum(1 for line in f if "[翻案]" in line)
        except Exception:
            over = 0
        return {"ok": True, "keys": len(L.nodes), "nodes": len(nodes),
                "v1": sum(1 for n in nodes if n.get("verdict") == 1),
                "v0": sum(1 for n in nodes if n.get("verdict") == 0),
                "overturns": over,
                "journal_bytes": os.path.getsize(JOURNAL) if os.path.exists(JOURNAL) else 0,
                "dir": STATE_DIR}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    def _after(argv, flag):
        """取 --flag 后面那个值（没有就 None）。"""
        try:
            i = argv.index(flag)
        except ValueError:
            return None
        return argv[i + 1] if i + 1 < len(argv) else None

    _args = sys.argv[1:]
    if _args and _args[0] == "stats":
        print(json.dumps(stats(), ensure_ascii=False))
    elif _args and _args[0] == "chain-head":
        _r = chain_head()
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    elif _args and _args[0] == "anchor":
        # 可选：anchor <锚文件路径>（默认 V2_ANCHORS / 账目录下的 anchors.jsonl）
        _p = _args[1] if len(_args) > 1 and not _args[1].startswith("--") else None
        _r = anchor(_p)
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    elif _args and _args[0] == "verify":
        # verify                       → 老口径：影子链全量重算
        # verify --against <锚文件>    → 拿本地链头对锚，能抓尾部被截断
        _ag = _after(_args, "--against")
        _r = verify_against(_ag) if _ag else verify_chain()
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    else:
        print(json.dumps({"ok": True, "ledger": _Ledger is not None, "dir": STATE_DIR}, ensure_ascii=False))
