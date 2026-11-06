"""v2_borrow.py —— 通用只读借账入口（2026-11-03）

这文件是**第一把开闸的钥匙**：只读借账，不改判决、不短路任何流程，
命中就把这一借写进流水（多进程谁也不盖谁），不手工改快照（避免盖掉写入方已写的行）。

用法:
    python v2_borrow.py demo           # 三路实测：新节借到 / 过期被拒 / 无记录
    python v2_borrow.py <key>          # 借某一节（单 key，现状行为）
    python v2_borrow.py <key> <原文>   # 借某一节 + 走语义借账路径 D（原文折候选，见下）
    python v2_borrow.py pages [n]      # 借账目录：按 hits 命中率降序（先命中过的页先查）
    python v2_borrow.py hot [n]        # 快扫：按目录顺序逐页翻，翻到能借的停
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import v2_gate as G            # noqa: E402  拿到 _Ledger / SNAP / JOURNAL / TTL

WRITER = os.environ.get("V2_WRITER", "v2-borrow")
__version__ = "2.0"            # 标准件版本号（2026-11-06 发布）


def load():
    if G._Ledger is None:
        raise RuntimeError("快照 ledger 缺失：scripts/v2_gate/ledger.py 没装上")
    return G._Ledger.load(G.SNAP, journal=G.JOURNAL, writer=WRITER, ttl=G.TTL, empty_ok=True)


# ══════════ 语义借账 · 路径 D：查询侧改写模板折叠（2026-11-05 推荐路）══════════
# 原 key miss 后，按**有限确定性模板**把原文折出 ≤4 个候选串，逐个 probe，任一借到即停；
# 全 miss 才落回 no_record。本节四关：
#   ① 0/1 不拍分数 —— 模板是有限表，每个候选非此即彼，无相似度、无阈值；
#   ② 判决语义零改动 —— probe 原样多调几次，三值/四因/hit 流水全不动；候选全 miss 时
#      why_not 仍取**原 key** 的 miss（四因可读，本节已知风险②）；
#   ③ 不碰 ganglion 骨、不碰 key 公式本体 —— 候选串照样交 ctx_fp.key_of() 算 key；
#   ④ 不碰真账 —— 只在查询侧 probe；候选层任何异常 → 整层吞掉退回单 key borrow（= 现状）。
MAX_CAND = 4                                            # 候选上限（本节四关定 ≤4 个）
NEG_WORDS = ("不", "别", "未", "勿", "禁止", "不可", "没")     # 含否定词：一个候选都不生成（宁可 miss）
TAIL_WORDS = "吗吧啊咧了呗哟嘛呢哈呀哦噢哎嗨"               # 句末语气词：加/去只认这张表
SYNONYMS = (("转帐", "转账"), ("帐号", "账号"), ("什么", "啥"), ("怎么", "咋"))   # 小同义表


def _candidates(msg):
    """确定性模板 → ≤4 个候选**串**（不是 key）。纯函数、无状态、0/1，无分数。

    底 = ctx_fp.normalize(msg)（去标点/空白/数字、截 64 —— 本节模板「去标点空白」
    就由这把尺给，与 key 公式同一把尺）。凑满 MAX_CAND 就停：
      ① 去句末语气词 → ② 小同义表（双向确定性替换）→ ③ 加句末语气词(吗吧啊咧) → ④ 去尾字
    含否定词（不/别/未/勿/禁止/不可/没）→ 直接 []：宁可 miss 不许错借，
    漂移方向只许往「多借不到」偏（本节已知风险①）。底算不出来也返回 []（fail-open）。
    """
    try:
        from ctx_fp import normalize
        s = normalize(msg)
    except Exception:
        return []
    if not s:
        return []
    for w in NEG_WORDS:
        if w in s:
            return []
    out = []

    def add(x):                       # 空的、和原文一模一样的、重复的都不收（原 key 不重复 probe）
        if len(out) < MAX_CAND and x and x != s and x not in out:
            out.append(x)

    t = s
    while t and t[-1] in TAIL_WORDS:  # ① 去句末语气词
        t = t[:-1]
    add(t)
    for a, b in SYNONYMS:             # ② 小同义表（双向、确定性、首个命中即用）
        if a in s:
            add(s.replace(a, b))
        elif b in s:
            add(s.replace(b, a))
    for p in "吗吧啊咧":               # ③ 加句末语气词
        add(s + p)
    if len(s) > 1:                    # ④ 去尾字
        add(s[:-1])
    return out


def _cand_key(cand):
    """候选串 → key：**同一把尺** ctx_fp.key_of()，key 公式本体一行不碰；算不出就 None（跳过）。"""
    try:
        from ctx_fp import key_of
        return key_of(cand)
    except Exception:
        return None


def borrow(key, ctx_fp="", min_age=None, msg=""):
    """借一节。返回 dict，绝不抛异常到调用方。

    msg：这一借的原文（可选）。不给 = 现状单 key 行为，输出一字不变；
    给了且原 key miss → 走语义借账路径 D：模板折 ≤4 个候选 key 逐个 probe，任一命中即停，
    借到的那节用 `via` 带出「借自哪个候选」（journal 的 hit 行记在候选 key 上，本节已知风险③）。
    """
    t0 = time.time()
    try:
        L = load()
        node = L.probe(key, ctx_fp, checked=True, min_age=min_age,
                       journal=G.JOURNAL, writer=WRITER)
        via = None
        if node is None and msg:
            try:                       # 候选层整段 fail-open：异常 → 吞掉，退回原 key 的 miss
                for c in _candidates(msg):
                    ck = _cand_key(c)
                    if ck is None or ck == key:
                        continue
                    cn = L.probe(ck, ctx_fp, checked=True, min_age=min_age,
                                 journal=G.JOURNAL, writer=WRITER)
                    if cn is not None:
                        node, via = cn, {"msg": c, "key": ck}
                        break          # 任一借到即停
            except Exception:
                pass
        out = {"ok": True, "key": key, "borrowed": node is not None,
               "why_not": None if node is not None else L.why_not(key),
               "node": node, "stat": dict(L.stat),
               "ms": round((time.time() - t0) * 1000, 1)}
        if via is not None:
            out["via"] = via
        return out
    except Exception as e:
        return {"ok": False, "key": key, "error": f"{type(e).__name__}: {e}"}


def _pick(L, want_fresh):
    """按账本自己的钟挑一节：新节 = _window_ok 窗内；过期节 = 出窗。"""
    now = L._clock()
    best = None
    for k, chain in L.nodes.items():
        for n in chain:
            if L.is_tombstone(n):
                continue
            try:
                fresh = bool(L._window_ok(n, now))
            except Exception:
                fresh = False          # 没钟的页：借不到，归到"过期"那侧
            if fresh != want_fresh:
                continue
            ttl = n.get("ttl") or 0
            age = now - (n.get("ts") or 0)
            cand = (abs(age - (ttl / 2 if want_fresh else 0)), k, n)
            if best is None or cand[0] < best[0]:
                best = cand
    return (best[1], best[2]) if best else (None, None)


# ══════════ 查询顺序：先查命中过的页（2026-11-05 快翻机制）══════════
def catalog():
    """借账目录：全账按 hits（命中次数）降序 —— 下次先查命中过的页。

    账攒出目录 = 数位即记忆地址：每一页都进目录，**0 也是资产**（没亮过的照收，
    只是排在亮页后面），书因此越翻越薄 —— 常命中的 key 永远最先被翻到。

    只排序、不判决：probe 的 0/1/None 三值、EXPIRED / no_record / mismatch
    一律不碰；这里连 miss 都不写，纯读。排序键：hits 降序 → last_hit 降序 → ts 降序。
    """
    L = load()
    now = L._clock()
    rows = []
    for k, chain in L.nodes.items():
        n = L._last_real(chain)
        if n is None:
            continue                        # 整条链只剩墓碑 = 空页，不进目录
        try:
            fresh = bool(L._window_ok(n, now))   # 和 probe 同一把钟；配对节点不同时以 probe 为准
        except Exception:
            fresh = False                   # 没钟的页：读得出、借不到 —— 只排不判
        rows.append({"key": k, "hits": int(n.get("hits") or 0),
                     "last_hit": n.get("last_hit"), "ts": n.get("ts"),
                     "fresh": fresh, "round": n.get("round"),
                     "verdict": n.get("verdict")})
    rows.sort(key=lambda r: (-r["hits"], -(r["last_hit"] or 0), -(r["ts"] or 0)))
    return rows


def borrow_hot(limit=None, ctx_fp="", min_age=None, stop_on_hit=True):
    """快扫：按目录顺序（hits 降序）逐页翻，**先翻亮过的页**，翻到能借的那页停。

    每一页都走同一个 borrow() —— 判决语义零改动（借不到的页 probe 只记 miss，
    不写流水；借到的那页写一行 hit，和手动借一模一样）。
    返回：翻过的顺序（每页带 hits）、翻了几页、其中亮页几页、借到的那一页。
    """
    all_rows = catalog()
    rows = all_rows[:int(limit)] if limit else all_rows
    tried, got, stopped = [], None, None
    for r in rows:
        b = borrow(r["key"], ctx_fp=ctx_fp, min_age=min_age)
        tried.append({"key": r["key"], "hits": r["hits"], "fresh": r["fresh"],
                      "borrowed": bool(b.get("borrowed")),
                      "why_not": b.get("why_not"), "ok": b.get("ok", False)})
        if b.get("ok") is False:
            stopped = b.get("error")        # 账读不动就别硬翻，断在这一页
            break
        if b.get("borrowed"):
            got = b
            if stop_on_hit:
                break
    lit_before = sum(1 for t in tried if t["hits"] > 0) if got else None
    return {"ok": True, "目录页数": len(all_rows), "翻过": len(tried),
            "亮页(hits>0)": sum(1 for t in tried if t["hits"] > 0),
            "借到前先翻的亮页": lit_before,
            "顺序": tried,
            "借到": ({"key": got["key"], "ms": got["ms"],
                      "why_not": got.get("why_not"),
                      "判决": (got.get("node") or {}).get("verdict")} if got else None),
            "中断": stopped}


def demo():
    L = load()
    now = L._clock()

    def _fresh(n):                      # 用账本自己的钟（_window_ok），别拿 ttl 字段硬算
        try:
            return bool(L._window_ok(n, now))
        except Exception:
            return False

    nodes = [n for c in L.nodes.values() for n in c if not L.is_tombstone(n)]
    fresh_n = sum(1 for n in nodes if _fresh(n))
    out = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "账面": {"keys": len(L.nodes), "活节": len(nodes),
                 "窗内": fresh_n, "出窗": len(nodes) - fresh_n,
                 "stat": dict(L.stat)},
        "三路": {},
    }

    # ① 窗内的节 —— 应该借到（带本节自己的 ctx_fp 去配，不然配不上=mismatch）
    fk, fn = _pick(L, True)
    out["三路"]["①窗内借"] = (borrow(fk, ctx_fp=(fn.get("ctx_fp") or ""))
                              if fk else {"skip": "没有窗内节"})

    # ② 出窗的节 —— 应该被拒（expired，同理带本节 ctx_fp，先过配对再验窗）
    ek, en = _pick(L, False)
    out["三路"]["②出窗拒"] = (borrow(ek, ctx_fp=(en.get("ctx_fp") or ""))
                              if ek else {"skip": "没有出窗节"})

    # ③ 账上没有的 key —— 应该拒（no_record）
    out["三路"]["③无记录拒"] = borrow("deadbeefdeadbeef")

    # 借到的那节，把判决原样带出来
    hit = out["三路"]["①窗内借"]
    if hit.get("borrowed") and hit.get("node"):
        out["借到的判决"] = {"verdict": hit["node"].get("verdict"),
                             "reason": hit["node"].get("reason"),
                             "round": hit["node"].get("round"),
                             "writer": hit["node"].get("writer")}

    before = sum(1 for _ in open(G.JOURNAL, encoding="utf-8")) if os.path.exists(G.JOURNAL) else 0
    out["流水行数_事后"] = before
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif len(sys.argv) > 1 and sys.argv[1] == "pages":
        # 目录读数：前 n 页（默认 10）——亮页在前，0 也在册
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        rows = catalog()
        print(json.dumps({"目录页数": len(rows), "亮页": sum(1 for r in rows if r["hits"] > 0),
                          "暗页(0也是资产)": sum(1 for r in rows if r["hits"] == 0),
                          "前n页": rows[:n]}, ensure_ascii=False, indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "hot":
        # 快扫读数：按 hits 降序翻，翻到能借的停
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(borrow_hot(limit=lim), ensure_ascii=False, indent=2, default=str))
    elif len(sys.argv) > 1:
        # 单 key（现状行为）；多给一个原文 = 走语义借账路径 D（模板折叠候选）
        print(json.dumps(borrow(sys.argv[1], msg=(sys.argv[2] if len(sys.argv) > 2 else "")),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": True, "ledger": G._Ledger is not None, "snap": G.SNAP}, ensure_ascii=False))
