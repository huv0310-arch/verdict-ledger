# -*- coding: utf-8 -*-
"""心跳骨节·主动开口 (v1)
心脏 5 分钟一跳, 这节骨每跳翻一遍账(dream_pending 挂账 + 3-60天前旧叙事),
翻到值得开口的(判 1)就把材料递给大模型张嘴; 没翻到 = 静默(0), 一分 token 不花。

骨节三值口径: 0=免费翻过去, 1=值得开口, None=材料拿不准就整条上交(宁可少说)。
本体是冻结的骨; 这里全是肉, 只长在反射端(两头延展口径)。
设计决定(2026-10-30): 心跳翻一次记忆, 判 1 则主动开口。
纪律: 1 必须挂真事件 + 冷却(默认3h), 不许自产自销; 材料文本禁止出现会漂移的
相对时间(否则监控每 tick 都判成变化 = 刷屏)。

静默设计: 空输出 = 没翻到 = 正常, 不是故障。怀疑断链: 手跑本脚本看输出与 state。
"""
import hashlib
import json
import os
import re
import sys
import time
import traceback

CRYPTO = os.environ.get("SPEAK_DATA_DIR") or os.path.expanduser("~/.speak_state")  # 状态目录可配
STATE_P = os.path.join(CRYPTO, "ganglion_speak_state.json")
LOG_P = os.path.join(CRYPTO, "ganglion_speak.log")
COOLDOWN_S = 60 * 60          # 两次开口最少隔 1h（冷却窗，原 3h 过长，按料池容量调）
COOLDOWN_TODAY_S = 30 * 60    # 念头源(今日)单列短冷却30min: 真念头不等1h(2026-11-05); 日封顶仍卡
DAILY_CAP = 10                # 一天最多开口10次, 封顶防刷屏(冷却1h时理论上限16, 压到10更像人)
EPISODIC_MIN_D, EPISODIC_MAX_D = 3, 60
DP_STALE_D = 7                 # dream_pending 超过这么久没人翻才值得提
EPISODIC_CAP = 28              # 窗内28个文件全带进来(原12只取到一半料, 重料概率高)
SPENT_KEEP = 200               # 已开口签名留最近 200 条
OUT_CAP = 380
JUDGE_P = os.path.join(CRYPTO, "ganglion_speak_judge.json")  # 判决落盘: 同文件永远免费

# —— 起念头源（『心跳起念』机制，2026-11-05）——
TODAY_DB = os.environ.get("V3_STATE_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "hermes", "state.db")  # 会话库可配
TODAY_WINDOW_S = 12 * 3600   # 最近12小时的我说过的话
TODAY_CAP = 12               # 念头候选最多12句(判决位挑一句)

_LINE_RE = re.compile(r"^\s*(?:[-*•]\s+|#+\s+|\d+[.)]\s+)?")


def _load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(p, d):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _sig(s):
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:10]


def _norm_line(ln):
    """剥掉 markdown 项目符号/标题井号, 返回干净的一句话(或空)。"""
    s = _LINE_RE.sub("", ln or "").strip()
    if not (15 <= len(s) <= 120):
        return ""
    if s.startswith(("http", "|", "```")):
        return ""
    return s


def _cands(body):
    """一个文件里所有够格的句子(按出现顺序)。判决位的输入。"""
    out = []
    for ln in body.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        s = _norm_line(ln)
        if s:
            out.append(s)
    return out


def _mimo_key():
    k = os.environ.get("LLM_API_KEY") or os.environ.get("XIAOMI_MIMO_API_KEY")
    if k:
        return k
    try:
        for _ln in open(os.environ.get("HERMES_ENV_FILE") or
                        os.path.expandvars(r"%LOCALAPPDATA%\hermes\.env"),
                        encoding="utf-8", errors="ignore"):
            if _ln.startswith("XIAOMI_MIMO_API_KEY="):
                return _ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _ask_mimo(prompt, max_tokens=90):
    """调一次 mimo-flash。失败返回空串(上层 fail-open 退回规则)。
    坑: mimo-v2.6-flash 是推理模型, 不关思考时 700 token 全烧在 reasoning 上,
    正文 finish_reason=length 一个字不出——必须 enable_thinking=False。"""
    import urllib.request
    key = _mimo_key()
    if not key:
        return ""
    url = os.environ.get("SPEAK_LLM_URL", "").strip()
    if not url:
        return ""          # 没配端点 = 不调模型，走 fail-open（发布默认关）
    body = json.dumps({
        "model": os.environ.get("SPEAK_LLM_MODEL", "mimo-v2.6-flash"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def _judge_batch(items):
    """骨节判决位(批量): 一次调用判完一批叙事文件, 每篇挑最值得跟进的一句+值分。
    items = [(fn, [候选句...]), ...]
    返回 {fn: (句子, 值分, 理由)}。
    判决落盘(JUDGE_P, key=整批哈希) -> 整批免费复用; 文件集一变才重判。
    fail-open: 整批失败 -> 返回 {}, 上层退回"取第一句"规则, 且不落盘(降级不冻结)。"""
    todo = []
    for fn, cands in items:
        if not cands:
            continue
        jkey = "b|" + _sig(fn + "|" + "|".join(cands))
        todo.append((fn, cands, jkey))
    if not todo:
        return {}
    batch_key = "batch|" + _sig("|".join(x[2] for x in todo))
    jd = _load(JUDGE_P, {}) or {}
    if batch_key in jd and isinstance(jd[batch_key], dict):
        return {k: tuple(v) for k, v in jd[batch_key].items()}   # 复用, 零 token

    blocks = []
    for i, (fn, cands, _k) in enumerate(todo):
        blocks.append("【%s】\n%s" % (fn[:10], "\n".join(
            "%d) %s" % (j + 1, c) for j, c in enumerate(cands))))
    prompt = ("你是用户的贴身助理, 在翻用户过去的每日叙事(每天一个块)。"
              "每个块挑出最值得主动跟用户提一句的一条(用户会感兴趣/有信息量/像人话), "
              "避开纯记账流水、纯数字统计、套话。\n"
              "背景: 通用场景, 不预设行业。\n"
              "只回结果, 每个块一行, 格式: <块名>|<该块内序号>|<值分0-3>|<理由8字内>。"
              "值分=值得说的程度(3很值得说)。块名照抄方括号里的名字。\n\n"
              + "\n\n".join(blocks))
    ans = _ask_mimo(prompt, max_tokens=2500)
    if not ans:
        return {}      # fail-open: 整批降级, 不落盘
    got = {}
    namemap = {fn[:10]: (fn, cands) for fn, cands, _k in todo}
    for ln in ans.splitlines():
        # 块名可能被模型包上【】/[]/（）, 全容忍
        m = re.search(
            r"[【\[（(]?\s*(\d{4}-\d{2}-\d{2})\s*[】\]）)]?\s*[|｜]\s*(\d+)\s*[|｜]\s*(\d)\s*[|｜]\s*(\S+)",
            ln)
        if not m:
            continue
        name, idx, val = m.group(1), int(m.group(2)) - 1, int(m.group(3))
        if name not in namemap:
            continue
        fn, cands = namemap[name]
        if not (0 <= idx < len(cands)):
            continue
        got[fn] = (cands[idx], val, m.group(4)[:12])
    if not got:
        return {}      # 答非所问, 不冻结
    # 只有整批判全才落盘——部分结果当完整结果落盘, 会让漏掉的文件永远停在退第一句
    if len(got) == len(todo):
        jd = {k: v for k, v in jd.items() if not str(k).startswith("batch|")}
        jd[batch_key] = {fn: list(t) for fn, t in got.items()}
        _save(JUDGE_P, jd)
    return got


def collect_dp():
    """挂账源: 压了一周没人翻的 dream_pending 提案。"""
    p = os.path.join(CRYPTO, "dream_pending.md")
    try:
        if not os.path.exists(p):
            return []
        age_d = (time.time() - os.path.getmtime(p)) / 86400.0
        if age_d <= DP_STALE_D:
            return []
        with open(p, encoding="utf-8") as f:
            body = f.read()
        n = len(re.findall(r"^\s*[-*]\s*\[ \]", body, re.M)) or \
            len(re.findall(r"^#{2,3} ", body, re.M))
        if not n:
            return []
        return [("dp|" + _sig(body[:400]),
                 "挂账: dream_pending 里有 %d 条提案压了超过一周, 还没人翻牌。" % n)]
    except Exception:
        return []


def collect_today():
    """起念头源: 最近12小时 state.db 里我说过的有内容的话（『心跳起念』机制，2026-11-05）。
    新鲜经历才有资格起话——同过 _judge_batch 判值分才出，出则排最前（起念优先）。
    读不到/出错一律 []（fail-open，绝不碍旧账料）。"""
    out = []
    try:
        if not TODAY_DB or not os.path.exists(TODAY_DB):
            return []
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % TODAY_DB, uri=True)
        try:
            rows = conn.execute(
                "SELECT content FROM messages WHERE role='assistant' AND timestamp>? "
                "ORDER BY timestamp DESC LIMIT 40",
                (time.time() - TODAY_WINDOW_S,)).fetchall()
        finally:
            conn.close()
        seen, cands = set(), []
        for (c,) in rows:
            if not isinstance(c, str):
                continue
            for ln in c.splitlines():
                s = _norm_line(ln)
                sg = _sig(s)
                # 感受后迭代(11-05): 执行碎片("跑一次""改完")不算起念, 只收≥25字的完整句
                if s and len(s) >= 25 and sg not in seen:
                    seen.add(sg)
                    cands.append(s)
                if len(cands) >= TODAY_CAP:
                    break
            if len(cands) >= TODAY_CAP:
                break
        if not cands:
            return []
        # 回应念(11-05): 混入最近 3 条对话——有话头才接, 互相了解是双向的
        try:
            conn2 = sqlite3.connect("file:%s?mode=ro" % TODAY_DB, uri=True)
            try:
                urows = conn2.execute(
                    "SELECT content FROM messages WHERE role='user' AND timestamp>? "
                    "ORDER BY timestamp DESC LIMIT 12",
                    (time.time() - TODAY_WINDOW_S,)).fetchall()
            finally:
                conn2.close()
            import re as _re
            for (c,) in urows:
                if not isinstance(c, str):
                    continue
                c1 = " ".join(c.split())
                # 滤系统注入(mem0/系统标记/平台注入等)与过短句
                if 15 <= len(c1) <= 160 and not _re.search(
                        # 过滤宿主平台的注入消息（按平台标记匹配，标记随平台走）
                        r"memory-context|Mem0 Memory|骨架·|本机代理前门|ASYNC|CONTEXT COMPR|System note|最近账尾|跨窗口", c1):
                    cands.append("用户说: " + c1)
                    if len(cands) >= TODAY_CAP + 3:
                        break
        except Exception:
            pass
        verdicts = _judge_batch([("today", cands)])
        pick, val, _why = verdicts.get("today", (cands[0], 0, ""))
        if pick:
            out.append(("today|" + _sig(pick), "念头: %s" % pick))
    except Exception:
        pass
    return out


def collect_episodic():
    """回忆源: 3-60 天前的每日叙事, 每篇由判决位挑一句最值得说的。
    按值分降序排(值分同则按文件名, 保证确定性)——高值分的料先开口。"""
    out = []
    try:
        epi = os.path.join(CRYPTO, "study", "episodic")
        if not os.path.isdir(epi):
            return []
        now = time.time()
        aged = []
        pool = []
        for fn in sorted(os.listdir(epi)):
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(epi, fn)
            d = (now - os.path.getmtime(fp)) / 86400.0
            if EPISODIC_MIN_D <= d <= EPISODIC_MAX_D:
                aged.append((fn, fp))
        for fn, fp in aged[:EPISODIC_CAP]:
            try:
                with open(fp, encoding="utf-8") as f:
                    body = f.read()
            except Exception:
                continue
            cands = _cands(body)
            if cands:
                pool.append((fn, cands))
        verdicts = _judge_batch(pool)   # 一次调用判完, 整批落盘复用
        for fn, cands in pool:
            pick, val, _why = verdicts.get(fn, (cands[0], 0, ""))  # 没判到退第一句
            if pick:
                sig = "epi|" + _sig(fn + "|" + pick)
                out.append((val, sig, "回忆[%s]: %s" % (fn[:10], pick)))
        out.sort(key=lambda x: (-x[0], x[1]))          # 值分降序, 同分按签名稳定
        out = [(sig, txt) for _v, sig, txt in out]
    except Exception:
        pass
    return out


def main():
    st = _load(STATE_P, {}) or {}
    spent = [x for x in (st.get("spent") or []) if isinstance(x, str)][-SPENT_KEEP:]
    held = st.get("held") or ""
    last_fire = float(st.get("last_fire") or 0.0)
    now = time.time()
    # 日封顶: 换天就清零当日开口计数
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if st.get("fire_day") != today:
        st["fire_day"], st["fire_count"] = today, 0
    # 冷却到了 + 当日没封顶, 才找下一条没说过的; 没新料就一直举着旧料(输出稳定 = monitor 不重复唤醒)
    if int(st.get("fire_count") or 0) < DAILY_CAP:
        # 冷却分层(11-05): 念头源30min、旧账源1h——真念头不被旧账的冷却压住
        today_pool = collect_today()
        recent_enough = (now - last_fire >= (COOLDOWN_TODAY_S if today_pool else COOLDOWN_S))
        if not recent_enough:
            today_pool = []   # 念头也在短冷却里 → 本轮只考虑旧账(且旧账要满1h)
        pool = today_pool + collect_dp() + collect_episodic() if (recent_enough and today_pool) else (
            collect_dp() + collect_episodic() if now - last_fire >= COOLDOWN_S else [])
        for sig, text in pool:
            if sig in spent:
                continue
            held = text[:OUT_CAP]
            spent.append(sig)
            last_fire = now
            st.update({"held": held, "held_sig": sig,
                       "spent": spent[-SPENT_KEEP:], "last_fire": last_fire,
                       "fire_day": today, "fire_count": int(st.get("fire_count") or 0) + 1,
                       "last_err": ""})
            _save(STATE_P, st)
            break
    if held:
        sys.stdout.write(held)


def _selftest():
    import io
    import tempfile
    global CRYPTO, STATE_P, LOG_P, JUDGE_P, TODAY_DB
    tmp = tempfile.mkdtemp(prefix="gs_selftest_")
    CRYPTO, STATE_P, LOG_P = tmp, os.path.join(tmp, "state.json"), os.path.join(tmp, "log.txt")
    JUDGE_P = os.path.join(tmp, "judge.json")   # 必须重定向, 否则自检污染真判决本
    TODAY_DB = os.path.join(tmp, "no_such.db")  # 念头源同样隔离: 自检不读真会话库
    epi = os.path.join(tmp, "study", "episodic")
    os.makedirs(epi)

    # 判决位自检时用打桩模型, 绝不碰网络
    global _ask_mimo
    _real_ask = _ask_mimo
    _ask_mimo = lambda prompt, max_tokens=90: ""   # 默认: 模型不在 -> 走 fail-open

    def run():
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            main()
        finally:
            sys.stdout = old
        return buf.getvalue()

    def mk(name, line, age_d):
        p = os.path.join(epi, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# %s 每日叙事\n\n- %s\n" % (name, line))
        t = time.time() - age_d * 86400
        os.utime(p, (t, t))

    # 1) 空账本 = 静默, 且不建 state
    assert run() == "", "空账本必须静默"
    assert not os.path.exists(STATE_P), "没开口不该写 state"
    ok = 1

    # 2) 真账形状(- 列表行)能抓到, 且两次 collect 一致(确定性)
    mk("2026-09-20.md", "那天我们把新部件装上了, 同事说这东西用起来顺手了不少。", 10)
    a, b = collect_episodic(), collect_episodic()
    assert a and a == b, "episodic collect 必须非空且确定: %r vs %r" % (a, b)
    ok += 1

    # 3) 开口一次 state 落盘; 第二次跑输出逐字节一致(monitor 不重复唤醒)
    out1 = run()
    assert out1.startswith("回忆["), "该开口没开口: %r" % out1
    st = _load(STATE_P)
    assert st and st.get("held_sig") and st.get("last_fire"), "state 没落盘"
    assert run() == out1, "同料重复变输出 = 刷屏隐患"
    ok += 1

    # 4) 冷却过后换新料(靠 spent 轮转), 旧签名不复用
    st["last_fire"] = time.time() - 4 * 3600
    _save(STATE_P, st)
    mk("2026-09-21.md", "换一天的另一句话, 长度得够十五个字符以上才行呀。", 9)
    out2 = run()
    assert out2 and out2 != out1, "冷却后应换新料: %r vs %r" % (out2, out1)
    assert _load(STATE_P)["held_sig"] in _load(STATE_P)["spent"], "开口签名该进 spent"
    ok += 1

    # 5) dream_pending 挂账源: 陈旧且非空才开口, 新鲜的不提
    dp = os.path.join(tmp, "dream_pending.md")
    with open(dp, "w", encoding="utf-8") as f:
        f.write("## 提案\n- [ ] 情绪值语义\n- [ ] None 主动举手\n")
    assert collect_dp() == [], "新鲜挂账不该开口"
    old_t = time.time() - 10 * 86400
    os.utime(dp, (old_t, old_t))
    c = collect_dp()
    assert c and c[0][1].startswith("挂账:"), "陈旧挂账源没抓到: %r" % c
    ok += 1

    # 6) 不满 3 天的新叙事不进窗
    mk("2026-10-29.md", "这是昨天的叙事, 不该被翻出来当旧账说话。", 1)
    assert not any("2026-10-29" in t for _, t in collect_episodic()), "3天内的新叙事不该进窗"
    ok += 1

    # 7) 日封顶: 当日开口到 DAILY_CAP 次后不再换新料(即使冷却放行)
    st = _load(STATE_P)
    st["fire_count"] = DAILY_CAP
    st["last_fire"] = time.time() - 4 * 3600   # 冷却已放行
    _save(STATE_P, st)
    before = run()
    st2 = _load(STATE_P)
    assert st2["fire_count"] == DAILY_CAP, "封顶后不该再开口换料: %r" % st2
    assert before == out2, "封顶后应保持原样不换料"
    ok += 1

    # 8) 换天清零: fire_day 不是今天 -> 计数清零并可再开口
    st2["fire_day"] = "2026-01-01"
    st2["fire_count"] = DAILY_CAP
    _save(STATE_P, st2)
    run()
    st3 = _load(STATE_P)
    assert st3["fire_day"] != "2026-01-01" and st3["fire_count"] <= 1, \
        "换天该清零重计: %r" % st3
    ok += 1

    # 9) 批量判决位 fail-open: 模型不在 -> 全批退回第一句+0分, 且【不落盘】(降级不冻结)
    c1 = _cands("标题行\n- 第一句够长了吧这是十五个字以上。\n- 第二句也够长了吧这是另一句话。\n")
    c2 = _cands("另一个标题\n- 甲句也够十五个字符以上了吧对。\n- 乙句也够十五个字符以上了吧对。\n")
    got = _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    assert got == {}, "fail-open 该返回空批: %r" % got
    assert not _load(JUDGE_P, {}), "fail-open 绝不该落盘: %r" % _load(JUDGE_P)
    ok += 1

    # 10) 批量判决位真答: 每块的序号各自生效, 值分生效, 且落盘
    _ask_mimo = lambda prompt, max_tokens=90: "2026-01-01|2|3|乙有意思\n2026-01-02|1|1|甲是重点"
    got = _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    assert got["2026-01-01.md"][0] == c1[1] and got["2026-01-01.md"][1] == 3, \
        "第1块该挑第2句+3分: %r" % (got.get("2026-01-01.md"),)
    assert got["2026-01-02.md"][0] == c2[0] and got["2026-01-02.md"][1] == 1, \
        "第2块该挑第1句+1分: %r" % (got.get("2026-01-02.md"),)
    assert _load(JUDGE_P, {}), "真判出来才该落盘"
    ok += 1

    # 11) 整批落盘=复用免费: 同一批再判不该调模型(打桩计数)
    calls = [0]
    def _counting(prompt, max_tokens=90):
        calls[0] += 1
        return "2026-01-01|1|1|占位"
    _ask_mimo = _counting
    _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    assert calls[0] == 0, "已判过的整批不该再调模型(复用已判结果应免费), 调了%d次" % calls[0]
    ok += 1

    # 12) 块名带方括号也要能匹配(模型真会这么回)
    _save(JUDGE_P, {})
    _ask_mimo = lambda prompt, max_tokens=90: "【2026-01-01】|2|3|括号也认\n[2026-01-02]|1|2|方括号也认"
    got = _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    assert got.get("2026-01-01.md", ("",))[0] == c1[1], "【】包块名该能匹配: %r" % got
    assert got.get("2026-01-02.md", ("",))[0] == c2[0], "[]包块名该能匹配: %r" % got
    ok += 1

    # 13) 部分批(漏判)绝不落盘——否则漏掉的文件永远停在退第一句
    _save(JUDGE_P, {})
    _ask_mimo = lambda prompt, max_tokens=90: "2026-01-01|1|1|只答了一个块"
    got = _judge_batch([("2026-01-01.md", c1), ("2026-01-02.md", c2)])
    assert len(got) == 1, "该只返回判到的1个: %r" % got
    assert not _load(JUDGE_P, {}), "部分批绝不能落盘(否则漏判被冻结): %r" % _load(JUDGE_P)
    ok += 1

    _ask_mimo = _real_ask
    print("selftest PASS %d/13" % ok)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    try:
        main()
    except Exception:
        try:
            with open(LOG_P, "a", encoding="utf-8") as f:
                f.write("%.0f %s\n" % (time.time(), traceback.format_exc()[-500:]))
        except Exception:
            pass
        # fail-open: 出错也不出声(静默不刷屏), 错落 log, 断链靠手跑排查
        sys.exit(0)
