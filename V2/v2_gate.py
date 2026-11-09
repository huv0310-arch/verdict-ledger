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
__version__ = "3.3"             # 对外发布 tag v3.3（2026-11-09）：Merkle 一致性证明补齐；沿革见 git log / CHANGELOG（对外发布时另附）
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
MERKLE_ALGO = "sha256-merkle"   # Merkle 根的算法标识（RFC 9162 的树形 + sha256），与链上根分开
MERKLE = os.environ.get("V2_MERKLE") or os.path.join(STATE_DIR, "merkle.jsonl")


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
        # Merkle 根与链上根同出一份流水（RFC 9162 树形）：给「不给行也能验」的证明当可信根
        res["merkle_root"] = "sha256:" + merkle_root(rows)
        if head and chain and chain[-1].get("h") != head:
            res["ok"] = False
            res["why"] = "影子链最后一行与 journal 重算的链头对不上（先跑 verify 看断在哪）"
        return res
    except Exception as e:
        return {"ok": False, "algo": ALGO, "lines": -1, "chained": -1,
                "head": "", "merkle_root": "", "why": str(e)[:120]}


def anchor(path=None):
    """把当前链头**签名后追加**写进独立锚文件（append-only：一行一锚，只增不改）。

    v3.2 起记录是**签名链头**：(entry_count, root_hash, issued_at, key_id) + 签名；
    v3.1 的老格式行 {ts,count,head,algo} 没有签名字段，按无签名继续读（向后兼容，旧行一个字节不改）。
    只写锚文件：journal 与 .chain 一个字节不改（这是只读接口的那半边）。
    链头读不出 / 账 0 行 / 没有密钥就不写 —— 没得锚不假装锚过，没签名不发锚。
    写之前先过一致性检查：新锚必须是旧锚的 append-only 延伸（条数不倒退、根跟着条数走）；
    同条数异根 = equivocation 收据 → 报红、不写。fail-open 同本文件其余接口。
    """
    ap = _anchor_path(path)
    try:
        h = chain_head()
        if not h.get("ok"):
            return {"ok": False, "path": ap, "why": h.get("why") or "链头读不出来"}
        if not h.get("lines"):
            return {"ok": False, "path": ap, "why": "账 0 行：没有可锚的链头"}
        key, kid, all_keys, kerr = _load_keys()
        if kerr:
            return {"ok": False, "path": ap, "why": kerr}
        if key is None:
            return {"ok": False, "path": ap,
                    "why": "没有当前密钥 V2_ANCHOR_KEY，签不了新锚（V2_ANCHOR_KEYS 只放旧钥，核验历史锚用）"}
        if os.path.exists(ap):
            chk = check_anchors(ap, recompute=True)
            if not chk["ok"]:
                return {"ok": False, "path": ap,
                        "why": "锚文件先没过一致性检查，不许再往上加：" + chk["why"],
                        "problems": chk.get("problems", []),
                        "equivocations": chk.get("equivocations", [])}
            old, _ = _read_anchors(ap)
            if old and h["lines"] < old[-1]["entry_count"]:
                return {"ok": False, "path": ap,
                        "why": "锚倒退：本地账 %d 行 < 上一枚锚的 %d 行（账被截断？先 verify --against 查）"
                               % (h["lines"], old[-1]["entry_count"])}
        issued = time.time()
        payload = {"entry_count": h["lines"], "root_hash": h["head"],
                   "issued_at": issued, "key_id": kid}
        rec = {"v": 2, "algo": ALGO}
        rec.update(payload)
        rec["sig"] = _hmac_sign(key, _canon(payload))
        d = os.path.dirname(ap)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(ap, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 同一段流水的 Merkle 根也签一份追加进 merkle.jsonl（给「不给行也能验」的证明当可信根）。
        # 写不进去不拖垮四字段锚 —— 如实回报，核验那一侧会报「验不了」，不冒充通过。
        mroot, merr = h.get("merkle_root") or "", ""
        try:
            if not mroot:
                merr = "链头没带 Merkle 根，Merkle 锚没写"
            else:
                merr = _append_merkle_anchor(h["lines"], h["head"], mroot, key, kid, issued)
        except Exception as e:
            merr = str(e)[:120]
        out = dict(rec)
        out.update({"ok": True, "path": ap,
                    "merkle_root": (mroot if not merr else ""),
                    "merkle_ok": not merr,
                    "why": "签名链头已追加进锚文件" + ("；Merkle 锚同签一份" if not merr
                                                   else "；Merkle 锚没写上：" + merr)})
        return out
    except Exception as e:
        return {"ok": False, "path": ap, "why": str(e)[:120]}


def verify_against(path=None):
    """拿本地链头对锚记录 —— 抓**尾部被截断**（journal + .chain 一起截也抓得到）。

    口径（四问，缺一不可）：
      · 锚文件不存在 / 是空的 → ok=False：没得比就是没验，绝不报绿；
      · 先过锚文件自身的一致性检查（签名核验 / 条数延伸性 / equivocation 自检）——
        锚文件自己不自洽就报红；v3.1 的老格式锚行没有签名字段，按无签名处理，不因此判红；
      · 本地行数 < 最新锚的行数 → 尾部被截断（truncated=True）；
      · 本地行数 >= 锚行数 → 把本地流水算到**锚那一行的位置**，链上哈希必须等于锚记的
        root_hash（对得上 = 锚点之前的账一个字没被动过；账在锚之后继续长是正常的）。
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
        chk = check_anchors(ap, recompute=False)
        if not chk["ok"]:
            return {"ok": False, "path": ap, "why": "锚文件核不过：" + chk["why"],
                    "problems": chk.get("problems", []),
                    "equivocations": chk.get("equivocations", []), "truncated": False}
        last = _anchor_norm(anchors[-1])
        cnt = last["entry_count"]
        rows = _journal_rows()
        local = len(rows)
        base = {"path": ap, "lines": local, "anchor": last["raw"], "truncated": False}
        if local < cnt:
            base.update({"ok": False, "truncated": True,
                         "why": f"尾部被截断：本地 {local} 行 < 锚记录 {cnt} 行"
                                f"（锚于 {last['issued_at']}）"})
            return base
        n, head = _head_of(rows, cnt)
        head = "sha256:" + head if head else ""
        if head != last["root_hash"]:
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


# ══════════ 签名链头 / 一致性 / 一致性证明挑战单（v3.2）══════════
# 锚记录升级为**签名链头**：(entry_count, root_hash, issued_at, key_id) + 签名，追加进锚文件。
# 签名走 stdlib 的 HMAC-SHA256 —— 标准库没有 ed25519，为签名引第三方依赖违背「零依赖」；
# 代价照实写：对称签名 = 持钥方也能签，钥必须握在写者之外的核验方手里才顶用；
# 非对称签名（ed25519 之类）是升级路径，本版没做。
# 老格式锚行（v3.1 的 {ts,count,head,algo}）没有签名字段 → 按无签名继续读，向后兼容、不改写旧行。
# 密钥只从环境变量 / 文件读，仓里不硬编码任何真密钥：
#   V2_ANCHOR_KEY      当前密钥字符串（签新锚、签挑战单与回执）
#   V2_ANCHOR_KEY_FILE 指向只放密钥的文件（与 V2_ANCHOR_KEY 二选一）
#   V2_ANCHOR_KEYS     轮换留下的旧密钥（逗号分隔），只用来核验历史单据
#   缺钥 → 拒发并说清怎么配；有签名行却没钥 → 报红，绝不静默放过。
PROOF_DEADLINE_S = 300   # 一致性证明的书面 deadline（秒）默认值；V2_PROOF_DEADLINE 覆盖


def _canon(obj):
    """规范化 JSON（键排序、无多余空格）：签名与核验必须逐字节同一份。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hmac_sign(key, body):
    """HMAC-SHA256 签名（hex）。key 是 bytes，body 是 _canon 出来的字符串。"""
    import hashlib
    import hmac
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def _key_id_of(key):
    """密钥指纹：只留哈希前 16 位 —— 密钥本身不进任何文件、不回显。"""
    import hashlib
    return "hmac-sha256:" + hashlib.sha256(key).hexdigest()[:16]


def _load_keys():
    """读签名密钥 → (当前钥, 当前 key_id, [核验用的全部钥], 错误文案)。

    没钥时错误文案说清怎么配；只有旧钥（V2_ANCHOR_KEYS）时当前钥为 None：签不了新的，核验照常。
    """
    raw = (os.environ.get("V2_ANCHOR_KEY") or "").strip()
    key_file = (os.environ.get("V2_ANCHOR_KEY_FILE") or "").strip()
    if not raw and key_file:
        try:
            with open(key_file, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError as e:
            return None, None, [], "签名密钥文件读不到：%s（%s）" % (key_file, e.strerror or e)
    olds = [x.strip() for x in (os.environ.get("V2_ANCHOR_KEYS") or "").split(",") if x.strip()]
    cur = raw.encode("utf-8") if raw else None
    all_keys = ([cur] if cur else []) + [o.encode("utf-8") for o in olds]
    if not all_keys:
        return None, None, [], ("签名密钥缺失：设 V2_ANCHOR_KEY，或用 V2_ANCHOR_KEY_FILE 指向只放密钥的文件"
                                "（锚与挑战单都要签名：不签名就不发，核验不过就报红）")
    return cur, (_key_id_of(cur) if cur else None), all_keys, ""


def _sig_ok(payload, sig, all_keys):
    """核验签名：拿全部可用钥逐个试（轮换期旧钥签的历史单据仍能核过）。"""
    import hashlib
    import hmac
    if not sig or not all_keys:
        return False
    body = _canon(payload).encode("utf-8")
    want = sig.encode("utf-8") if isinstance(sig, str) else sig
    for k in all_keys:
        if hmac.compare_digest(hmac.new(k, body, hashlib.sha256).hexdigest().encode("utf-8"), want):
            return True
    return False


def _anchor_payload(n):
    """被签名的四个字段（签名链头）：核验时从锚行里原样重建同一份。"""
    return {"entry_count": n["entry_count"], "root_hash": n["root_hash"],
            "issued_at": n["issued_at"], "key_id": n["key_id"]}


def _anchor_norm(rec):
    """新旧两种锚行归一到同一套字段；老格式（v3.1）没有签名字段 → sig=None，按无签名处理。"""
    if not isinstance(rec, dict):
        raise ValueError("锚行不是对象")
    cnt = rec.get("entry_count", rec.get("count"))
    root = rec.get("root_hash", rec.get("head"))
    if cnt is None or root is None:
        raise ValueError("锚行缺 entry_count / root_hash（新旧两种格式都不像）")
    return {"entry_count": int(cnt), "root_hash": str(root),
            "issued_at": rec.get("issued_at", rec.get("ts")),
            "key_id": rec.get("key_id"), "sig": rec.get("sig"),
            "algo": rec.get("algo") or ALGO, "raw": rec, "line": 0}


def _read_anchors(path):
    """读锚文件 → (归一行列表, 错误)。文件不存在 → ([], None)，空文件同。"""
    ap = _anchor_path(path)
    if not os.path.exists(ap):
        return [], None
    rows, err = [], None
    with open(ap, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                n = _anchor_norm(json.loads(line))
            except Exception as e:
                err = "锚文件第 %d 行读不了：%s" % (i, str(e)[:90])
                break
            n["line"] = i
            rows.append(n)
    return rows, err


def check_anchors(path=None, recompute=True):
    """锚文件一致性检查 + equivocation 自检（只读）。

    报红的五种：
      ① 坏行 / 有签名行却没钥 / 签名核不过（事后被动过，或钥不对）；
      ② 条数倒退 —— 新锚不是旧锚的 append-only 延伸；
      ③ 条数变了 root_hash 没动 —— 链头记录自相矛盾；
      ④ 同 entry_count、不同 root_hash 的两个签名头 → **equivocation 收据**：
         明确报警、不静默，两个头按文件原样列出来供审计者复算；
      ⑤ recompute=True 且本地有流水：任一锚的 root_hash 与本地重算对不上，
         或锚条数超过本地账行数（账被截断 / 锚不来自本账）。
    v3.1 老格式无签名行不参与签名核验（按无签名处理），其余检查一视同仁。
    返回 {ok, path, anchors, problems, equivocations, notes, why}
    """
    ap = _anchor_path(path)
    res = {"ok": True, "path": ap, "anchors": 0, "problems": [],
           "equivocations": [], "notes": [], "why": ""}
    if not os.path.exists(ap):
        res.update(ok=False, problems=["锚文件不存在：没有锚可查"], why="锚文件不存在：没有锚可查")
        return res
    rows, err = _read_anchors(ap)
    if err:
        res.update(ok=False, problems=[err], why=err)
        return res
    if not rows:
        res.update(ok=False, problems=["锚文件是空的：没有锚可查"], why="锚文件是空的：没有锚可查")
        return res
    res["anchors"] = len(rows)

    # ① 签名核验
    key, kid, all_keys, kerr = _load_keys()
    signed = [n for n in rows if n["sig"]]
    if signed and not all_keys:
        res["problems"].append("锚文件里有 %d 行签名，但核验不了：%s" % (len(signed), kerr))
    elif signed:
        for n in rows:
            if n["sig"] and not _sig_ok(_anchor_payload(n), n["sig"], all_keys):
                res["problems"].append("第 %d 行签名核不过：这一行事后被动过，或钥不对（key_id=%s）"
                                       % (n["line"], n["key_id"]))
    unsigned = len(rows) - len(signed)
    if unsigned:
        res["notes"].append("%d 行是 v3.1 老格式（无签名），按无签名处理" % unsigned)

    # ② ③ 延伸性：条数只许往前长，根必须跟着条数走
    for i in range(1, len(rows)):
        p, n = rows[i - 1], rows[i]
        if n["entry_count"] < p["entry_count"]:
            res["problems"].append("锚倒退：第 %d 行 entry_count=%d < 第 %d 行的 %d（append-only 只许往前长）"
                                   % (n["line"], n["entry_count"], p["line"], p["entry_count"]))
        elif n["entry_count"] > p["entry_count"] and n["root_hash"] == p["root_hash"]:
            res["problems"].append("锚跳跃：条数从 %d 长到 %d，root_hash 却一动没动（链头记录自相矛盾）"
                                   % (p["entry_count"], n["entry_count"]))

    # ④ equivocation：同条数异根 = 自包含的作弊收据（同条数同根 = 重复锚，不算分叉）
    by_count = {}
    for n in rows:
        bucket = by_count.setdefault(n["entry_count"], {})
        if n["root_hash"] in bucket:
            continue
        for other in bucket.values():
            res["equivocations"].append({"entry_count": n["entry_count"],
                                         "a_line": other["line"], "a": other["raw"],
                                         "b_line": n["line"], "b": n["raw"]})
        bucket[n["root_hash"]] = n
    for e in res["equivocations"]:
        res["problems"].append("equivocation 收据：同 entry_count=%d 出现不同 root_hash"
                               "（锚文件第 %d 行与第 %d 行）—— 两个签名头原样列出，供审计者复算"
                               % (e["entry_count"], e["a_line"], e["b_line"]))

    # ⑤ 本地重算（有流水才做）
    if recompute:
        if os.path.exists(JOURNAL):
            local = _journal_rows()
            for n in rows:
                if n["entry_count"] > len(local):
                    res["problems"].append("第 %d 行 entry_count=%d 超过本地账 %d 行：本地账被截断，"
                                           "或这枚锚不来自本账" % (n["line"], n["entry_count"], len(local)))
                else:
                    _, head = _head_of(local, n["entry_count"])
                    if ("sha256:" + head if head else "") != n["root_hash"]:
                        res["problems"].append("第 %d 行 root_hash 与本地重算对不上：锚那一段的账被动过"
                                               % n["line"])
        else:
            res["notes"].append("本地没有流水，跳过重算（只做格式 / 签名 / 延伸性检查）")

    if res["problems"]:
        res["ok"] = False
        res["why"] = res["problems"][0] + ("（另有 %d 条）" % (len(res["problems"]) - 1)
                                           if len(res["problems"]) > 1 else "")
    else:
        res["why"] = "锚文件一致性通过：%d 枚锚，条数延伸与签名都对%s" % (
            len(rows), "；" + "；".join(res["notes"]) if res["notes"] else "")
    return res


# ══════════ 不给行也能验：Merkle 一致性证明（RFC 9162）══════════
# 缺口出处：README 边界里写着「不给行也能验的 Merkle 一致性证明还没做」——这一段把它补上。
# 做法（纯标准库，树形按 RFC 9162 §2.1.1 / 证明按 §2.1.4）：
#   · 叶子 = 该行规范化 JSON 的 sha256(0x00 || 行)；节点 = sha256(0x01 || 左 || 右)；
#     n>1 时 k = 小于 n 的最大 2 的幂 —— 树形只由条数决定，两边各自算必然同形。
#   · 一致性证明 = 从 from 端的根走到 to 端的根所需的 O(log n) 个中间节点，**一个原始行都不给**；
#     核验方按 §2.1.4.2 复算，两枚根必须与自己手里的锚一致才算过。
#   · 可信根从哪来：anchor 在写四字段锚的同一刻，把同一条数、同一 root_hash 的 Merkle 根
#     签名追加进 merkle.jsonl（append-only，V2_MERKLE 可挪位置）；四字段锚一个字节没改。
#   · 绑定口径：Merkle 记录必须与四字段锚**同条数、同 root_hash** 才参与核验，
#     同条数异根的 Merkle 记录不采信（分叉检测仍归 check-anchors 的 equivocation 自检）。
#   · 边界照实说：from=0 是空前缀，本来就一致（只核 to 端的锚）；merkle 锚缺记录 → 报「验不了」，
#     绝不冒充通过；它与锚同机同盘，流水 + 链 + 锚 + Merkle 锚一起改写的共谋仍在边界之外。


def _merkle_path(path=None):
    return path or MERKLE


def _m_hash(data):
    """sha256 → 小写十六进制。"""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _strip_sha(s):
    s = "" if s is None else str(s)
    return s[7:] if s.startswith("sha256:") else s


def _mth(leaves, lo, hi, cache):
    """MTH(leaves[lo:hi])（RFC 9162 §2.1.1）：空表 = HASH()；单叶 = 叶哈希；
    n>1 → HASH(0x01 || MTH(左 k) || MTH(右 n-k))，k = 小于 n 的最大 2 的幂。"""
    n = hi - lo
    if n <= 0:
        return _m_hash(b"")
    if n == 1:
        return leaves[lo]
    if (lo, hi) in cache:
        return cache[(lo, hi)]
    k = 1 << (((n - 1).bit_length()) - 1)
    v = _m_hash(b"\x01" + bytes.fromhex(_mth(leaves, lo, lo + k, cache))
                + bytes.fromhex(_mth(leaves, lo + k, hi, cache)))
    cache[(lo, hi)] = v
    return v


def _leaves_of(rows):
    """叶子哈希列表：每行规范化 JSON 前面挂 0x00（与节点的 0x01 分域）。"""
    return [_m_hash(b"\x00" + _canon(r).encode("utf-8")) for r in rows]


def merkle_root(rows):
    """整段流水的 Merkle 根（不带前缀的十六进制）。"""
    leaves = _leaves_of(rows)
    return _mth(leaves, 0, len(leaves), {})


def _subproof(leaves, m, lo, hi, complete, out, cache):
    """RFC 9162 §2.1.4.1 的 SUBPROOF（绝对下标写法，m >= 1 才会进来）。"""
    n, rel = hi - lo, m - lo
    if rel == n:                       # 走到 from 端那棵完整的子树
        if not complete:
            out.append(_mth(leaves, lo, hi, cache))
        return
    k = 1 << (((n - 1).bit_length()) - 1)
    if rel <= k:
        _subproof(leaves, m, lo, lo + k, complete, out, cache)
        out.append(_mth(leaves, lo + k, hi, cache))     # 右半边只在 to 端存在：给个承诺
    else:
        _subproof(leaves, m, lo + k, hi, False, out, cache)
        out.append(_mth(leaves, lo, lo + k, cache))     # 左半边两边同形：给个承诺


def merkle_consistency_proof(rows, from_count, to_count):
    """生成一致性证明的节点列表（不含任何原始行）。返回 (nodes, 错误)。"""
    try:
        f_cnt, t_cnt = int(from_count), int(to_count)
    except (TypeError, ValueError):
        return None, "from / to 必须是整数条数"
    if f_cnt < 1 or t_cnt <= f_cnt or t_cnt > len(rows):
        return None, ("口径要求 0 < from < to <= 本地行数（收到 from=%s to=%s，本地 %d 行）"
                      % (from_count, to_count, len(rows)))
    leaves = _leaves_of(rows[:t_cnt])
    out = []
    _subproof(leaves, f_cnt, 0, t_cnt, True, out, {})
    return out, ""


def check_merkle_consistency(first, second, first_hash, second_hash, path):
    """RFC 9162 §2.1.4.2 的核验算法，逐条照抄。返回 (ok, why)。"""
    try:
        first, second = int(first), int(second)
        fh, sh = _strip_sha(first_hash), _strip_sha(second_hash)
        if not path:
            return False, "一致性证明是空的（RFC 9162 §2.1.4.2 第 1 步：空路径直接判失败）"
        if first <= 0 or second <= first:
            return False, "口径要求 0 < from < to（收到 from=%s to=%s）" % (first, second)
        if len(fh) != 64 or len(sh) != 64:
            return False, "两枚根不是 64 位十六进制（收到 %d / %d 位）" % (len(fh), len(sh))
        nodes = list(path)
        if first & (first - 1) == 0:            # from 条数是 2 的幂：先把 from 端的根补进序列
            nodes = [fh] + nodes
        fn, sn = first - 1, second - 1
        while fn & 1:                           # 第 4 步：把 fn 的低位 1 右移干净
            fn >>= 1
            sn >>= 1
        fr = sr = nodes[0]
        for c in nodes[1:]:
            if sn == 0:
                return False, "证明没走完就到头了（sn 提前归 0）"
            if (fn & 1) or fn == sn:
                fr = _m_hash(b"\x01" + bytes.fromhex(c) + bytes.fromhex(fr))
                sr = _m_hash(b"\x01" + bytes.fromhex(c) + bytes.fromhex(sr))
                while fn and not (fn & 1):      # 第 6b(iii) 步
                    fn >>= 1
                    sn >>= 1
            else:
                sr = _m_hash(b"\x01" + bytes.fromhex(sr) + bytes.fromhex(c))
            fn >>= 1
            sn >>= 1
        if fr != fh:
            return False, "from 端的根复算对不上"
        if sr != sh:
            return False, "to 端的根复算对不上"
        if sn != 0:
            return False, "证明没走完（sn=%d）" % sn
        return True, "Merkle 一致性证明复算通过（%d 个节点，一个原始行都没给）" % len(nodes)
    except Exception as e:
        return False, "Merkle 一致性证明核验出错：%s" % str(e)[:90]


def _merkle_payload(rec):
    """被签名的五个字段（Merkle 锚）：核验时从记录里原样重建同一份。"""
    return {"entry_count": rec["entry_count"], "root_hash": rec["root_hash"],
            "merkle_root": rec["merkle_root"], "issued_at": rec["issued_at"],
            "key_id": rec["key_id"]}


def _append_merkle_anchor(count, root_hash, mroot, key, kid, issued, path=None):
    """Merkle 锚追加进 merkle.jsonl（append-only，签名）。返回错误文案（空 = 成功）。"""
    mp = _merkle_path(path)
    rec = {"v": 1, "algo": MERKLE_ALGO, "entry_count": int(count), "root_hash": root_hash,
           "merkle_root": mroot, "issued_at": issued, "key_id": kid}
    rec["sig"] = _hmac_sign(key, _canon(_merkle_payload(rec)))
    d = os.path.dirname(mp)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(mp, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return ""


def _read_merkle_anchors(path=None):
    """读 merkle.jsonl → (记录列表, 错误)。文件不存在 → ([], None)。"""
    mp = _merkle_path(path)
    if not os.path.exists(mp):
        return [], None
    rows, err = [], None
    with open(mp, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if not isinstance(r, dict) or r.get("entry_count") is None or not r.get("merkle_root"):
                    raise ValueError("缺 entry_count / merkle_root")
            except Exception as e:
                err = "Merkle 锚文件第 %d 行读不了：%s" % (i, str(e)[:80])
                break
            r["_line"] = i
            rows.append(r)
    return rows, err


def _anchored_root(count, anchors_path=None):
    """四字段锚里第 count 条的 root_hash → (root_hash, 错误)。"""
    arows, aerr = _read_anchors(anchors_path)
    if aerr:
        return None, aerr
    a = next((x for x in arows if x["entry_count"] == int(count)), None)
    if a is None:
        return None, "锚文件里没有第 %s 条的签名链头：核验方没有可信链头" % count
    return a["root_hash"], ""


def _merkle_binding(count, anchors_path=None, merkle_path=None):
    """找「与四字段锚同条数、同 root_hash」的 Merkle 锚 → (记录, 错误)。
    缺记录 / 签名核不过 / 根对不上，一律报错 —— 不冒充通过。"""
    root, rerr = _anchored_root(count, anchors_path)
    if rerr:
        return None, rerr
    mrows, merr = _read_merkle_anchors(merkle_path)
    if merr:
        return None, merr
    cands = [r for r in mrows
             if int(r["entry_count"]) == int(count) and r.get("root_hash") == root]
    if not cands:
        return None, "Merkle 锚里没有第 %s 条这条链头的记录（先 anchor 记一笔再答复）" % count
    rec = cands[-1]                       # 同条数同根的重复记录取最后一条
    key, kid, all_keys, kerr = _load_keys()
    if not all_keys:
        return None, "核验不了 Merkle 锚签名：" + kerr
    if not _sig_ok(_merkle_payload(rec), rec.get("sig"), all_keys):
        return None, "第 %s 条的 Merkle 锚签名核不过（这一行事后被动过，或钥不对）" % rec["_line"]
    return rec, ""


def _build_merkle_proof(f_cnt, t_cnt, anchors_path=None, merkle_path=None):
    """不给行的一致性证明：两枚根取自 Merkle 锚（与四字段锚绑定），节点按 RFC 9162 生成。"""
    rows = _journal_rows()
    if t_cnt > len(rows):
        return None, "本地账只有 %d 行，凑不出 from=%d to=%d 的一致性证明" % (len(rows), f_cnt, t_cnt)
    t_rec, terr = _merkle_binding(t_cnt, anchors_path, merkle_path)
    if terr:
        return None, terr
    f_root, nodes, perr = "sha256:" + _m_hash(b""), [], ""   # from=0：空树（MTH({}) = HASH()）
    if f_cnt >= 1:
        f_rec, ferr = _merkle_binding(f_cnt, anchors_path, merkle_path)
        if ferr:
            return None, ferr
        f_root = f_rec["merkle_root"]
        nodes, perr = merkle_consistency_proof(rows, f_cnt, t_cnt)
        if perr:
            return None, perr
    return {"algo": MERKLE_ALGO, "kind": "consistency",
            "from_count": f_cnt, "to_count": t_cnt,
            "from_root": f_root, "to_root": t_rec["merkle_root"], "nodes": nodes}, ""


def _check_merkle_proof(proof, anchors_path=None, merkle_path=None):
    """复算「不给行」的证明：两枚根必须与锚绑定得上，再按 RFC 9162 §2.1.4.2 走一遍。"""
    try:
        f_cnt, t_cnt = int(proof.get("from_count")), int(proof.get("to_count"))
    except (TypeError, ValueError):
        return False, "证明里没有 from_count / to_count"
    if f_cnt < 0 or t_cnt <= f_cnt:
        return False, "口径要求 0 <= from < to（收到 from=%s to=%s）" % (f_cnt, t_cnt)
    nodes = proof.get("nodes")
    if not isinstance(nodes, list):
        return False, "证明里没有 nodes：这不是 Merkle 一致性证明"
    t_rec, terr = _merkle_binding(t_cnt, anchors_path, merkle_path)
    if terr:
        return False, terr
    if proof.get("to_root") and _strip_sha(proof["to_root"]) != _strip_sha(t_rec["merkle_root"]):
        return False, "证明里的 to 端根与 Merkle 锚对不上"
    if f_cnt == 0:
        # 空前缀与任何账都一致（RFC 的算法从 0 < first 起）：只核 to 端的锚，节点必须是空的
        if nodes:
            return False, "from=0 是空前缀，不该带节点（收到 %d 个）" % len(nodes)
        if proof.get("from_root") and _strip_sha(proof["from_root"]) != _m_hash(b""):
            return False, "from=0 的根不是空树哈希"
        return True, "from=0：空前缀与任何账都一致（to 端的 Merkle 锚签名核过）"
    f_rec, ferr = _merkle_binding(f_cnt, anchors_path, merkle_path)
    if ferr:
        return False, ferr
    if proof.get("from_root") and _strip_sha(proof["from_root"]) != _strip_sha(f_rec["merkle_root"]):
        return False, "证明里的 from 端根与 Merkle 锚对不上"
    return check_merkle_consistency(f_cnt, t_cnt, f_rec["merkle_root"], t_rec["merkle_root"], nodes)


# ── 一致性证明挑战单（challenges.jsonl，append-only 事件流）────────────────
# 外部审计当场追的那一问（deadline 定多久 / 判失败前留存什么）在这里给代码级答案：
#   deadline 默认 300 秒（V2_PROOF_DEADLINE 覆盖）；理由与改法写在 README。
#   判「拒绝证明」之前必须同时留住：签名请求单 + 送达回执 —— 缺回执一律不判失败。
# 文件一行一事件（request / delivery / response），同 request_id 折叠成一张挑战单视图，
# 视图字段就是对外口径：{request_id, from_count, to_count, issued_at, deadline,
#   request_body, signature, delivered_at, response, responded_at | timeout}


def _challenge_path(path=None):
    return path or os.environ.get("V2_CHALLENGES") or os.path.join(STATE_DIR, "challenges.jsonl")


def proof_deadline():
    """一致性证明的书面 deadline（秒）：默认 300，V2_PROOF_DEADLINE 覆盖。

    非法值不静默改数 → 返回 (None, 错误文案)，调用方报红。
    """
    raw = (os.environ.get("V2_PROOF_DEADLINE") or "").strip()
    if not raw:
        return float(PROOF_DEADLINE_S), ""
    try:
        v = float(raw)
    except ValueError:
        return None, "V2_PROOF_DEADLINE 必须是正数（秒），收到：%r" % raw
    if v <= 0:
        return None, "V2_PROOF_DEADLINE 必须大于 0（秒），收到：%r" % raw
    return v, ""


def _append_challenge(cp, row):
    d = os.path.dirname(cp)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(cp, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fold_challenges(path=None):
    """事件流折叠成挑战单视图（按首次出现顺序）。返回 (视图列表, 错误)。"""
    cp = _challenge_path(path)
    if not os.path.exists(cp):
        return [], None
    rows, order, views, err = [], [], {}, None
    with open(cp, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if not isinstance(r, dict) or not r.get("type") or not r.get("request_id"):
                    raise ValueError("缺 type / request_id")
            except Exception as e:
                err = "challenges.jsonl 第 %d 行读不了：%s" % (i, str(e)[:90])
                break
            rows.append(r)
    if err:
        return [], err
    for r in rows:
        rid, t = r["request_id"], r.get("type")
        if t == "request":
            if rid in views:
                continue                      # append-only：同单只认第一张请求，不覆盖
            views[rid] = {"request_id": rid, "from_count": r.get("from_count"),
                          "to_count": r.get("to_count"), "issued_at": r.get("issued_at"),
                          "deadline": r.get("deadline"), "request_body": r.get("body"),
                          "signature": r.get("signature"), "key_id": r.get("key_id"),
                          "delivered_at": None, "delivery_signature": None,
                          "response": None, "responded_at": None, "response_signature": None}
            order.append(rid)
        elif rid in views:
            v = views[rid]
            if t == "delivery" and v["delivered_at"] is None:
                v["delivered_at"] = r.get("delivered_at")
                v["delivery_signature"] = r.get("signature")
            elif t == "response" and v["response"] is None:
                v["response"] = r.get("response")
                v["responded_at"] = r.get("responded_at")
                v["response_signature"] = r.get("signature")
    return [views[r] for r in order], None


def challenge(from_count, to_count, path=None):
    """发一张一致性证明挑战单：日志要在 deadline 内出示 consistency_proof(from, to)。

    落盘的是**签名请求单**（append-only）；送达回执是另一笔事件（challenge_deliver），
    判「拒绝证明」之前两样都得在。
    """
    cp = _challenge_path(path)
    try:
        try:
            f_cnt, t_cnt = int(from_count), int(to_count)
        except (TypeError, ValueError):
            return {"ok": False, "path": cp, "why": "from / to 必须是整数条数"}
        if f_cnt < 0 or t_cnt <= f_cnt:
            return {"ok": False, "path": cp,
                    "why": "要求 0 <= from < to（收到 from=%s to=%s）" % (from_count, to_count)}
        key, kid, all_keys, kerr = _load_keys()
        if kerr:
            return {"ok": False, "path": cp, "why": kerr}
        if key is None:
            return {"ok": False, "path": cp, "why": "没有当前密钥 V2_ANCHOR_KEY，请求单签不了名"}
        dl, derr = proof_deadline()
        if dl is None:
            return {"ok": False, "path": cp, "why": derr}
        import secrets
        issued = time.time()
        rid = "ch-%d-%s" % (int(issued), secrets.token_hex(4))
        payload = {"request_id": rid, "from_count": f_cnt, "to_count": t_cnt,
                   "issued_at": issued, "deadline": dl, "key_id": kid}
        body = _canon(payload)
        row = {"type": "request", "request_id": rid, "from_count": f_cnt, "to_count": t_cnt,
               "issued_at": issued, "deadline": dl, "body": body,
               "signature": _hmac_sign(key, body), "key_id": kid}
        _append_challenge(cp, row)
        return {"ok": True, "path": cp, "request_id": rid, "from_count": f_cnt,
                "to_count": t_cnt, "issued_at": issued, "deadline": dl,
                "request_body": body, "signature": row["signature"], "key_id": kid,
                "delivered_at": None, "response": None, "responded_at": None,
                "why": "挑战单已签发（deadline %s 秒）；下一步留回执：challenge-deliver %s" % (dl, rid)}
    except Exception as e:
        return {"ok": False, "path": cp, "why": str(e)[:120]}


def challenge_deliver(request_id, path=None, at=None):
    """送达回执：把 delivered_at 追加成一笔**签名事件**（判失败的前置凭据之一）。

    本地实现里，请求单落进双方共读的 append-only 文件就是送达；
    跨机器部署时这一笔必须由收到方回签，不能自己给自己回执。
    """
    cp = _challenge_path(path)
    try:
        views, err = _fold_challenges(path)
        if err:
            return {"ok": False, "path": cp, "why": err}
        v = next((x for x in views if x["request_id"] == request_id), None)
        if v is None:
            return {"ok": False, "path": cp, "why": "没有这张挑战单：%s（先 challenge 签发）" % request_id}
        if v["delivered_at"] is not None:
            return {"ok": False, "path": cp, "why": "回执已留存（append-only，不重复记）：%s" % request_id}
        key, kid, all_keys, kerr = _load_keys()
        if kerr:
            return {"ok": False, "path": cp, "why": kerr}
        if key is None:
            return {"ok": False, "path": cp, "why": "没有当前密钥 V2_ANCHOR_KEY，回执签不了名"}
        when = time.time() if at is None else float(at)
        body = _canon({"request_id": request_id, "delivered_at": when})
        row = {"type": "delivery", "request_id": request_id, "delivered_at": when,
               "body": body, "signature": _hmac_sign(key, body), "key_id": kid}
        _append_challenge(cp, row)
        return {"ok": True, "path": cp, "request_id": request_id, "delivered_at": when,
                "why": "送达回执已留存并签名"}
    except Exception as e:
        return {"ok": False, "path": cp, "why": str(e)[:120]}


def _build_proof(f_cnt, t_cnt):
    """一致性证明本体（带行的形态）：两端链头 + 中间的行 + 中间的影子链哈希。

    核验方拿 from 端的链头，逐行重算到 to 端，必须落在答复里的 head_to 上。
    不想给行的场合走 _build_merkle_proof（RFC 9162，见上一段）。
    """
    rows = _journal_rows()
    if t_cnt > len(rows):
        return None, "本地账只有 %d 行，凑不出 from=%d to=%d 的一致性证明" % (len(rows), f_cnt, t_cnt)
    head_from = _head_of(rows, f_cnt)[1]
    head_to = _head_of(rows, t_cnt)[1]
    chain = []
    if os.path.exists(_CHAIN):
        with open(_CHAIN, encoding="utf-8") as f:
            chain = [json.loads(x) for x in f if x.strip()]
    seg = [c for c in chain if isinstance(c.get("n"), int) and f_cnt <= c["n"] < t_cnt]
    proof = {"algo": ALGO, "from_count": f_cnt, "to_count": t_cnt,
             "head_from": ("sha256:" + head_from) if head_from else "",
             "head_to": ("sha256:" + head_to) if head_to else "",
             "rows": rows[f_cnt:t_cnt], "chain": seg}
    return proof, ""


def check_proof(proof, anchors_path=None, merkle_path=None):
    """复算一致性证明，两种形态都认：
      · 带 rows 的老形态 —— 从 head_from 逐行算到 head_to，链段必须步步对上；
      · 带 nodes 的 Merkle 形态（不给行）—— 两枚根必须与 Merkle 锚绑定得上，
        再按 RFC 9162 §2.1.4.2 复算 O(log n) 个中间节点；锚缺了就报「验不了」。
    返回 (ok, why)。"""
    if isinstance(proof, dict) and ("nodes" in proof
                                    or str(proof.get("algo") or "").startswith(MERKLE_ALGO)):
        return _check_merkle_proof(proof, anchors_path, merkle_path)
    try:
        if not isinstance(proof, dict):
            return False, "答复里没有证明"
        f_cnt, t_cnt = int(proof["from_count"]), int(proof["to_count"])
        rows = proof.get("rows") or []
        if t_cnt - f_cnt != len(rows):
            return False, "证明的行数对不上 from/to"
        prev = proof.get("head_from") or ""
        prev = prev[7:] if prev.startswith("sha256:") else prev
        seg = {c.get("n"): c.get("h") for c in (proof.get("chain") or [])}
        for i, row in enumerate(rows):
            n = f_cnt + i
            prev = _line_h(prev, row)
            if n in seg and seg[n] != prev:
                return False, "影子链第 %d 行与重算对不上" % n
        want = proof.get("head_to") or ""
        want = want[7:] if want.startswith("sha256:") else want
        if prev != want:
            return False, "从 head_from 重算不到 head_to"
        return True, "证明复算通过（%d 行）" % len(rows)
    except Exception as e:
        return False, "证明复算出错：%s" % str(e)[:80]


def challenge_respond(request_id, path=None, at=None, merkle_only=False):
    """日志方答复：附一致性证明、签名后追加（append-only）。

    merkle_only=True → 只给 Merkle 一致性证明（**一个原始行都不给**）：
    两枚根必须已经在 merkle.jsonl 里签好名，凑不出就如实报错、不写答复。
    默认（merkle_only=False）给带行的老形态，老核验照旧。

    凑不出证明（本地账不够 to 条）就如实报错、不写答复 —— 那种情况下
    超时判定仍按 challenges.jsonl 里的回执字段走，不由这里说了算。
    """
    cp = _challenge_path(path)
    try:
        views, err = _fold_challenges(path)
        if err:
            return {"ok": False, "path": cp, "why": err}
        v = next((x for x in views if x["request_id"] == request_id), None)
        if v is None:
            return {"ok": False, "path": cp, "why": "没有这张挑战单：%s" % request_id}
        if v["response"] is not None:
            return {"ok": False, "path": cp, "why": "已答复过（append-only，不重复记）：%s" % request_id}
        if merkle_only:
            proof, perr = _build_merkle_proof(int(v["from_count"]), int(v["to_count"]))
        else:
            proof, perr = _build_proof(int(v["from_count"]), int(v["to_count"]))
        if perr:
            return {"ok": False, "path": cp, "why": perr}
        key, kid, all_keys, kerr = _load_keys()
        if kerr:
            return {"ok": False, "path": cp, "why": kerr}
        if key is None:
            return {"ok": False, "path": cp, "why": "没有当前密钥 V2_ANCHOR_KEY，答复签不了名"}
        when = time.time() if at is None else float(at)
        body = _canon({"request_id": request_id, "response": proof, "responded_at": when})
        row = {"type": "response", "request_id": request_id, "response": proof,
               "responded_at": when, "body": body,
               "signature": _hmac_sign(key, body), "key_id": kid}
        _append_challenge(cp, row)
        return {"ok": True, "path": cp, "request_id": request_id, "responded_at": when,
                "response": proof, "why": "答复已签发并签名"}
    except Exception as e:
        return {"ok": False, "path": cp, "why": str(e)[:120]}


def _challenge_sigs_ok(v, all_keys):
    """核验一张挑战单上的三个签名（请求单 / 送达回执 / 答复）。返回问题列表（空 = 全过）。"""
    probs = []
    if v.get("signature"):
        p = {"request_id": v["request_id"], "from_count": v["from_count"],
             "to_count": v["to_count"], "issued_at": v["issued_at"],
             "deadline": v["deadline"], "key_id": v["key_id"]}
        if not _sig_ok(p, v["signature"], all_keys):
            probs.append("请求单签名核不过（单据被动过，或钥不对）")
    if v.get("delivered_at") is not None and v.get("delivery_signature"):
        if not _sig_ok({"request_id": v["request_id"], "delivered_at": v["delivered_at"]},
                       v["delivery_signature"], all_keys):
            probs.append("送达回执签名核不过")
    if v.get("response") is not None and v.get("response_signature"):
        if not _sig_ok({"request_id": v["request_id"], "response": v["response"],
                        "responded_at": v["responded_at"]}, v["response_signature"], all_keys):
            probs.append("答复签名核不过")
    return probs


def challenge_status(request_id=None, path=None, now=None):
    """查单：状态一律按 challenges.jsonl 里的字段现算，不靠谁口头承诺。

    状态口径（写在代码里，不是只写文档）：
      answered      已答复，且在 deadline 内
      answered_late 过了 deadline 才答复（留痕，不等于拒绝证明）
      pending       已送达、还没到 deadline，等答复
      undelivered   **缺送达回执 → 不判失败**（退出码 0）
      timeout       有回执 + 过了 issued_at + deadline + 没答复 → 判「拒绝证明」（退出码 1）
    请求单/回执/答复签名核不过、文件坏 → 报红（退出码 1），绝不静默。
    返回 {ok, judged_timeout, challenges, path, why}
    """
    cp = _challenge_path(path)
    try:
        views, err = _fold_challenges(path)
        if err:
            return {"ok": False, "judged_timeout": False, "challenges": [], "path": cp, "why": err}
        if request_id is not None and not any(v["request_id"] == request_id for v in views):
            return {"ok": False, "judged_timeout": False, "challenges": [], "path": cp,
                    "why": "没有这张挑战单：%s" % request_id}
        if not views:
            return {"ok": True, "judged_timeout": False, "challenges": [], "path": cp,
                    "why": "还没有挑战单（challenge <from> <to> 先发一张）"}
        now = time.time() if now is None else now
        key, kid, all_keys, kerr = _load_keys()
        out, counts, bad = [], {}, 0
        for v in views:
            if request_id is not None and v["request_id"] != request_id:
                continue
            row = dict(v)
            issued = float(v.get("issued_at") or 0.0)
            try:
                dl = float(v.get("deadline")) if v.get("deadline") is not None else PROOF_DEADLINE_S
            except (TypeError, ValueError):
                dl = PROOF_DEADLINE_S
            due = issued + dl
            row["due_at"] = round(due, 3)
            if v.get("response") is not None:
                row["status"] = "answered" if float(v.get("responded_at") or 0.0) <= due else "answered_late"
                row["why"] = "答复已留存（%s）" % v.get("responded_at")
            elif v.get("delivered_at") is None:
                row["status"] = "undelivered"
                row["why"] = ("缺送达回执：按口径不判失败 —— 先把回执留住（challenge-deliver %s）"
                              % v["request_id"])
            elif now > due:
                row["status"] = "timeout"
                row["why"] = ("拒绝证明成立：签名请求单 + 送达回执（%s）都在，issued_at+deadline=%s 已过，"
                              "没有答复 —— 这张单据本身就是收据" % (v["delivered_at"], round(due, 3)))
            else:
                row["status"] = "pending"
                row["why"] = "已送达，deadline 内等答复（还剩 %.0f 秒）" % max(0.0, due - now)
            sig_probs = _challenge_sigs_ok(v, all_keys) if all_keys else (
                ["没有可用密钥，单据签名核不了：%s" % kerr] if v.get("signature") else [])
            if sig_probs:
                row["status"] = "bad_signature"
                row["why"] = "；".join(sig_probs)
                bad += 1
            elif row["status"] == "answered":
                ok_p, why_p = check_proof(v["response"])
                row["proof_ok"], row["proof_why"] = ok_p, why_p
                if not ok_p:
                    row["status"] = "bad_proof"
                    row["why"] = "答复里的证明复算不过：" + why_p
                    bad += 1
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            out.append(row)
        judged = counts.get("timeout", 0) > 0
        why = "；".join("%s×%d" % (k, counts[k]) for k in sorted(counts))
        if judged:
            why += "；已判「拒绝证明」的单据在 challenges.jsonl 里原样留存"
        return {"ok": (not judged) and bad == 0, "judged_timeout": judged,
                "challenges": out, "path": cp, "why": why}
    except Exception as e:
        return {"ok": False, "judged_timeout": False, "challenges": [], "path": cp,
                "why": str(e)[:120]}


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

    def _first_positional(argv):
        """取第一个位置参数：跳过 --flag 及其取值（本文件的长 flag 都带一个取值）。"""
        skip = False
        for a in argv[1:]:
            if skip:
                skip = False
                continue
            if a.startswith("--"):
                skip = True
                continue
            return a
        return None

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
    elif _args and _args[0] == "check-anchors":
        # check-anchors [--against <锚文件>] [--no-recompute]
        # 一致性检查（条数延伸 / 重算）+ equivocation 自检：报警时退出码 1
        _p = _after(_args, "--against")
        if _p is None and len(_args) > 1 and not _args[1].startswith("--"):
            _p = _args[1]
        _r = check_anchors(_p, recompute="--no-recompute" not in _args)
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    elif _args and _args[0] == "challenge":
        # challenge <from> <to> [--path <challenges.jsonl>]   → 签发一致性证明挑战单
        _p = _after(_args, "--path")
        if len(_args) < 3 or _args[1].startswith("--") or _args[2].startswith("--"):
            _r = {"ok": False, "why": "用法：challenge <from> <to> [--path <challenges.jsonl>]"}
        else:
            _r = challenge(_args[1], _args[2], path=_p)
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    elif _args and _args[0] in ("challenge-deliver", "challenge-respond"):
        # challenge-deliver <request_id>                  → 送达回执（判失败前必须留存）
        # challenge-respond <request_id>                  → 日志方答复（带行的老形态）
        # challenge-respond <request_id> --merkle-only    → 答复只给 Merkle 证明（一个原始行都不给）
        def _ticket_id(argv):
            """取 request_id：跳过 --path 的取值，也跳过不带取值的开关（如 --merkle-only）。"""
            out, skip = [], False
            for a in argv[1:]:
                if skip:
                    skip = False
                    continue
                if a == "--path":
                    skip = True
                    continue
                if a.startswith("--"):
                    continue
                out.append(a)
            return out[0] if out else None
        _p = _after(_args, "--path")
        _rid = _ticket_id(_args)
        if _args[0] == "challenge-deliver":
            _r = challenge_deliver(_rid, path=_p)
        else:
            _r = challenge_respond(_rid, path=_p, merkle_only="--merkle-only" in _args)
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    elif _args and _args[0] == "challenge-status":
        # challenge-status [<request_id>] [--path <challenges.jsonl>]
        # 超时判定只看 challenges.jsonl 里的回执字段：缺回执不判失败（0），判了拒绝证明（1）
        _p = _after(_args, "--path")
        _r = challenge_status(_first_positional(_args), path=_p)
        print(json.dumps(_r, ensure_ascii=False))
        sys.exit(0 if _r.get("ok") else 1)
    else:
        print(json.dumps({"ok": True, "ledger": _Ledger is not None, "dir": STATE_DIR}, ensure_ascii=False))
