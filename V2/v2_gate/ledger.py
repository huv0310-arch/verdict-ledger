"""ledger.py —— 判决账本（V2 通用件）

骨（ganglion.py，105 行）一行不动。这里只长"账"的那一半。

    check() 出去的永远是三个值当中的一个：
        1        判是（塌缩了）
        0        判否（塌缩了）
        Pending  正在进行中（没塌缩）—— 不是"没有答案"，是"答案还在长"

一节的形状：
    点    verdict      判了什么       1 / 0（空不许落这里）
    轴    round        第几回          只增不改
          value_score  值多少          未回填必须是 None，不是 0
          hits         被翻过几次
    指纹  ctx_fp       上次什么情况下问的

三条不能动的：
    ① 空不入账        —— 判不了的那次不占一格，只留悬位
    ② round 只增不改  —— 覆盖一次就永久丢一次翻面的证据
    ③ 配不上不许借    —— key 命中只证明"问过同一件事"

三个必须你自己填（本文件一个默认值都不给）：
    ttl     你的结论多久过期          不传 = 本账只记不借
    deliver 判不了的时候交给谁        不传 = 悬位只留在账上
    ctx_fp  什么算"同一个情形"        每次调用时传，不替你定

一句话不该忘的：接口可以加，面永远是六个（值/判决/缓存/节/时刻/可借；该文件见 github.com/huv0310-arch/ganglion 仓库 docs/LEDGER.md）。
新加的方法只是换个姿势穿过那六个面，不是第七个门。

用法：

    L = Ledger(ttl=None, deliver=print)               # 想借账，先交出你的窗——窗由调用方按圈距现场算，不查表
    g = LedgerGanglion(L, journal=False)
    v, why = g.check("某条内容", processor=my_fn, ctx_fp=my_fp(...))

    L.save("led.json")          # 落盘；不落盘的账本，重启一次就归零
    L = Ledger.load("led.json", ttl=..., deliver=...)
"""

import hashlib
import glob
import shutil
import json
import os
import uuid
import threading
import time
import sys

# ── 三值 ──────────────────────────────────────────────────────
YES = 1
NO = 0

# 悬位的三种质地 —— 不是三种空，是三个方向
MISSING = "missing"      # 无料之空：缺字段、缺上下文 → 该补料
EVIDENCE = "evidence"    # 无据之空：料齐了判据不够硬 → 该升档或交人
MISMATCH = "mismatch"    # 无配之空：账里有旧节，就是配不上 → 这不是空，是该重算

def addr(text):
    """给外部用的地址生成器：同一个字符串永远同一个地址。

    内部那个 _make_key 是私货，外人想算同款地址只能去抄 —— 抄私货等于把
    内部实现当了 API（外部评审A 2026-10-31 报的正是这条）。这里给一个正式的。
    """
    try:
        from ganglion import _make_key
    except ImportError as e:
        raise ImportError(
            "addr() 需要外挂的反射标准件 ganglion.py（独立仓库，本发布包不携带）："
            "请把 ganglion.py 与本文件装在同一目录（或加入 PYTHONPATH）后再调用。"
        ) from e
    return _make_key(text)


# probe() 没借到的四种原因
NO_RECORD = "no_record"  # 这个地址还没人问过
EXPIRED = "expired"      # 有旧节，但过窗了
TOO_FRESH = "too_fresh"  # 刚判完就问 —— 同一瞬间的抖动不该被当成结论
# 第三种就是上面的 MISMATCH：有旧节，窗内，但这次的上下文配不上


def make_ctx_fp(*parts):
    """配对指纹：前序上下文的摘要。

    这只是一个现成的拼法，不是规矩——粗到会话、细到轮次、还是更细，
    只有你知道"什么算同一个情形"。嫌不对，自己写一个函数传进来。
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Pending:
    """悬位：正在进行中的那一次。

    它不是一条判决，所以不占账上的一格。
    但"还在走"本身要有痕迹，否则"还在走"和"从没发生"在账上长得一模一样。
    记三样：差什么、走到第几圈、试过几次 —— 这三个数就是那根轴的方向。
    """

    __slots__ = ("why", "need", "key", "round", "tries", "ctx_fp", "first_ts", "last_ts",
                 "decide_by", "forced", "writer")

    def __init__(self, why, need="", key="", round=1, tries=1, ctx_fp="", now=None,
                 decide_by=None, writer=""):
        if why not in (MISSING, EVIDENCE, MISMATCH):
            raise ValueError("悬位的质地只能是 missing / evidence / mismatch")
        self.why = why
        self.need = need
        self.key = key
        self.round = round
        self.tries = tries
        self.ctx_fp = ctx_fp
        self.first_ts = time.time() if now is None else now
        self.last_ts = self.first_ts
        self.decide_by = decide_by      # 到点必须收口；None = 一直等料
        self.forced = False             # 是被时间逼着收口的（到点无据）
        self.writer = writer            # 谁开的这笔悬位（空 = 旧账，没有署名）

    @property
    def needs_recompute(self):
        """配不上不是"我判不了"，是"我该去判" —— 别把它往上交。"""
        return self.why == MISMATCH

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        p = cls(d["why"], d.get("need", ""), d.get("key", ""),
                d.get("round", 1), d.get("tries", 1), d.get("ctx_fp", ""), d.get("first_ts"))
        p.last_ts = d.get("last_ts", p.first_ts)
        p.decide_by = d.get("decide_by")
        p.forced = bool(d.get("forced", False))
        p.writer = d.get("writer", "")      # 旧账没署名，读回来是空 —— 不替它编一个
        return p

    def __repr__(self):
        return f"<Pending {self.why} 第{self.round}圈 试{self.tries}次 {self.need!r}>"

    # 三值里"空"也是空，方便使用者写 if v is None or isinstance(v, Pending)
    def __bool__(self):
        return False


class LedgerBlocked(Exception):
    """想借账，却没交出你的决断。

    账本不给默认保鲜期 —— 那个数一给，你抄一遍就能跑，
    跑出来的是别人的判断，不是你的。
    """


class Ledger:
    def __init__(self, writer=None, ttl=None, deliver=None, now=None):
        """
        writer  : 这本账是谁在写。**不传就必须每节署名** —— 没有默认值。
                  一本没有写者的账，出事时谁都能说"不是我"。
        ttl     : callable(node)->秒，或一个秒数。不传 = 只记不借（probe 会拒）
        deliver : callable(Pending)->None。判不了的时候交给谁。不传 = 只留在账上
        now     : 测试用时钟，一般不用传
        """
        # 窗必须是正数 / callable / None。0 或负数 = 想在账上留一个
        # "永不过期"的后门，装载时就拒（契约 3.1 / 8.3）。
        if ttl is not None and not callable(ttl) and ttl <= 0:
            raise ValueError(f"ttl 必须是正数 / callable / None，收到 {ttl!r}")
        self.writer = writer
        self.ttl = ttl
        self.deliver = deliver
        self._clock = now or time.time
        self._journal_upto = 0      # 流水正本读到哪个字节了（快照里带着它，重演不重复）
        self._compacted = []        # 哪些压实备份已经吃进快照了（剩下的还要重演）
        self._sealed = {}           # 已消费备份读到第几个字节：备份后来又长了，就接着读尾巴

        self.nodes = {}      # key -> [第1节, 第2节, ...]
        self.open = {}       # key -> Pending（还没塌缩的那一次）
        self.miss = {}       # key -> probe 没借到的原因
        self.stat = {"req": 0, "borrowed": 0, "computed": 0, "unresolved": 0, "unknown": 0}
        # 收口计数按"哪一件"去重：resolve 写一行、回放又数一遍，同一笔收口会被算两次，
        # 于是"收口了几笔"随存盘次数往上长（第一批对外数字就是假的）。
        self._resolved_seen = set()
        self._hit_seen = set()

    # ══════════ 写：塌缩的落账，没塌缩的留悬位 ══════════
    def _who(self, writer=None):
        """这节账谁写的。**没有默认值** —— 不署名就不许落账。

        一本没有写者的账，出事时谁都能说"不是我"。署名不是为了追责，
        是为了让"我记得我们判过"这句话能被查：谁判的、哪一轮、凭什么。
        """
        w = self.writer if writer is None else writer
        if w is None or not str(w).strip():
            raise ValueError(
                "这节账谁写的？—— 署名没有默认值。声明一次 Ledger(writer=\"谁\")，"
                "或每节 record(..., writer=\"谁\")。")
        return str(w).strip()

    def record(self, key, verdict, reason="", ctx_fp="", ts=None, value_score=None, ttl=None,
               writer=None, cross_fp=""):
        """一节账。verdict 只能是 1 或 0 —— 空不许落这里。"""
        # 每节自带窗也一样：0 / 负数当场拒，不许落进账里再躺着。
        if ttl is not None and not callable(ttl) and ttl <= 0:
            raise ValueError(f"本节 ttl 必须是正数 / callable / None，收到 {ttl!r}")
        who = self._who(writer)
        if verdict is None or isinstance(verdict, Pending):
            raise ValueError(
                "空不入账：判不了的那一次不占一格。"
                "落一次空，整本账的'没发生'就都不可信了。用 open_pending() 记悬位。"
            )
        # 判决零校验是伪造历史最便宜的入口（2026-10-31 两份外部评审都指到这儿）：
        # record 只挡了空，'对' / 2 / -1 / True 全都收，账上判决栏就写着 '对'，
        # 随后 antipode() 直接崩。三条路（record / append / check）必须共用同一条尺。
        if type(verdict) is not int or verdict not in (0, 1):
            raise ValueError(
                f"判决只能是 1 或 0（整数），收到 {verdict!r}。"
                "字符串一律不收：'对'/'错'/'true' 不是判决，是话。"
                "判不了就别记这一节 —— 用 open_pending() 留悬位。")
        chain = self.nodes.setdefault(key, [])
        node = {
            "key": key,
            "verdict": verdict,
            "reason": reason,
            "ts": self._clock() if ts is None else ts,
            "round": len(chain) + 1,      # 只增不改
            "value_score": value_score,   # 未回填 ≠ 0 分
            "ctx_fp": ctx_fp,
            # 公共坐标（各端同值的轨道指纹）。**只在给了值时才写这一栏** ——
            # 不给就不写，免得给全账凭空添一栏空串（schema_check 会报「有键但空着」）。
            **({"cross_fp": cross_fp} if cross_fp else {}),
            "hits": 0,
            "last_hit": None,
            "borrows": [],                # 抄了必留痕：抄的哪一圈、抄的时候核没核
            "ttl": ttl,                   # 这一节自己的钟：快了慢了自己定，全账共用一个表就是错
            "writer": who,                # 谁写的：不署名不许落账（没有默认值）
            # 身份：一节账从生到死就这一个号，重演、合并、归档都靠它认人。
            # 以前靠"比内容"认人 —— 只要这节在内存里被改过（回填分、补指纹、被墓碑化），
            # 内容就对不上，自己那行重演时当成新的一节追加：判决史翻倍、删掉的节复活。
            "nid": uuid.uuid4().hex,
        }
        if node["ttl"] is None and self.ttl is not None:
            # 写入时就把当时生效的窗固化到这一节。不固化的话，账本级的 ttl
            # 一改，所有历史节的含意跟着变 —— 那等于改了对过去的解释。
            w = self.ttl
            node["ttl"] = w(node) if callable(w) else w
        chain.append(node)
        self.open.pop(key, None)          # 塌缩了，悬位收掉
        return node

    def open_pending(self, key, why=EVIDENCE, need="", ctx_fp="", now=None, decide_by=None,
                     writer=None):
        if not isinstance(key, str) or not key:
            raise ValueError(f"悬位也要有地址：key={key!r} 不是内容摘要。")
        """记一次"还在走"。同一条再悬一次是 tries+1，不是新开一个。

        decide_by: 到点必须收口。不给 = 一直等料（也是一种正当态度，但要你自己认）。
        """
        prev = self.open.get(key)
        rnd = len(self.nodes.get(key, [])) + 1
        if prev is None:
            p = Pending(why, need, key, rnd, 1, ctx_fp, self._clock() if now is None else now,
                        decide_by=decide_by, writer=self._who(writer))
            self.open[key] = p
        else:
            p = prev
            p.tries += 1
            p.why = why
            p.need = need or p.need
            p.ctx_fp = ctx_fp or p.ctx_fp
            p.last_ts = self._clock() if now is None else now
            if decide_by is not None:
                p.decide_by = decide_by
        self.stat["opened"] = self.stat.get("opened", 0) + 1   # 累计开过几笔悬位
        if self.deliver is not None:
            self.deliver(p)               # 空必须有出口，没有出口的空等于静默失败
        return p

    def resolve(self, key, verdict=None, reason="", ctx_fp="", value_score=None,
                *, journal, forced=False, writer=None):
        """把一个悬着的位置收掉。

        悬位开出来容易，收回去一直是含糊的：以前只能"再记一节 0"顺带把它挤掉，
        结果"我恢复了"和"这件事了结了"混成一句话。这两个不是一回事：

            resolve(key)              只是销账（比如眼睛回来了，但没得出判决）
            resolve(key, 1, "续保了")  了结 + 落一节账，一步做完

        journal : **必填，没有默认值**（和 probe 同一条规矩）。给路径 = 收口这件事
                  追加一行，重放时对得上；forced=True 的"时间逼的收口"尤其要留痕 ——
                  不然进程一崩，这笔怎么没的就没人知道了。真不要落账就明写
                  journal=False（只许单进程）。
        """
        if journal is None or journal == "":
            raise ValueError(
                "收口也要落账：resolve(..., journal=<流水路径>)；"
                "确实不要落账就明写 journal=False（只许单进程）")
        p = self.open.pop(key, None)
        now = self._clock()
        if p is not None and (key, now) not in self._resolved_seen:
            self._resolved_seen.add((key, now))
            self.stat["resolved"] = self.stat.get("resolved", 0) + 1
            if forced:                    # 时间逼的：到点还没判出来，这笔要留痕
                p.forced = True
                self.stat["forced"] = self.stat.get("forced", 0) + 1
        node = None
        if verdict is not None:
            node = self.record(key, verdict, reason=reason, ctx_fp=ctx_fp,
                               value_score=value_score, writer=writer)
        if journal is not False:
            # had=True/False 只有读着账的进程才知道；短命进程手上没有悬位，
            # 它写 None = "写的时候我不知道"，由回放时的真实状态来定，不许瞎报。
            line = {"t": "resolve", "key": key, "ts": now,
                    # 我手上这笔就是从 self.open 里 pop 出来的，知道就是知道：
                    # 以前写成 (p is not None) if self.open else None —— pop 完账上没别的悬位时
                    # 明明知道也记 None，于是"收口了几笔"永远漏掉"唯一那一笔"。
                    "had": True if p is not None else None,
                    "forced": bool(forced),
                    "by": self._who(writer)}      # 收口是谁收的，也要认得出
            if node is not None:
                line["node"] = node
            self._write_line(journal, line)
        return p

    def revisit(self, key, rnd, verdict, reason="", ctx_fp=None, value_score=None,
                writer=None, journal=None):
        """拿今天的判据，重演账上第 rnd 圈。

        旧节一个字不许改 —— 改了就等于"我们从来都这么判的"，那是抹账。
        新落一节标 revisit_of=rnd，两节并存。

        于是 flips() 能分出两种翻面：
            情况变了（普通新圈）  —— 世界动了
            判据变了（revisit）   —— 我们动了
        分不开这两种，"翻过几次"就只是个数字。
        """
        src = next((n for n in self.nodes.get(key, []) if n["round"] == rnd), None)
        if src is None:
            raise ValueError(f"第 {rnd} 圈不存在：翻不到一节没判过的账")
        node = self.record(key, verdict, reason=reason,
                           ctx_fp=src["ctx_fp"] if ctx_fp is None else ctx_fp,
                           value_score=value_score, writer=writer)
        node["revisit_of"] = rnd
        node["mirror_ts"] = src["ts"]        # 被重演的那一节是什么时刻判的
        # 重演出来的这一节也得能落流水：不然短命进程里 revisit 完进程一退，
        # 账上只剩快照里那一节，流水里查不到"我们动过判据"。
        if journal:
            self._write_line(journal, {"t": "node", "key": key, "node": dict(node, round=None)})
        node["mirror_verdict"] = src["verdict"]
        return node

    def due(self, now=None):
        """到点该收口的悬位：时间到了还没判出来，不许它一直挂着装没事。"""
        now = self._clock() if now is None else now
        return [p for p in self.open.values()
                if p.decide_by is not None and now >= p.decide_by]

    def append(self, path, key, verdict=None, reason="", ctx_fp="", cross_fp="",
               # 与 record() 的关系：record 记在内存（常驻进程用），append 直接追加一行
               # （短命进程用）。同一个 key 二选一，别混用 —— 混了就是两套地址，
               # 回放时对不上（外部评审A 2026-10-31 实测踩到：节点在明文地址下、流水在哈希地址下）。
               why=EVIDENCE, value_score=None, ttl=None, writer=None):
        """记一节账并只追加一行 —— 不读全本，也不重写全本。

        给短命进程用：一分钟跑一次的东西，没道理每次都把整本账读进来再写回去。
        verdict=1/0 是落一节账；verdict=None 是记一次悬位（reason 当"差什么"）。

        副作用照记（这一进程内存里的 self 也长），但真正的正本在流水文件里。
        """
        # 参数错位护栏（2026-10-31 外部评审B外部实测撞到的）：
        # append 的第一个参数是流水路径、第二个是 key。少写一位路径，就会把地址当文件名、
        # 写出一条 key=null 的垃圾记账 —— 而且**不报错**，脏得看不出来。
        if not isinstance(key, str) or not key:
            raise ValueError(
                "append 的第二个参数是 key（账的地址，内容摘要）—— 第一个参数是流水路径。"
                f"收到 key={key!r}：参数错位了。正确写法 append(path, key, verdict, reason, ctx_fp)。")
        if not (isinstance(path, str) and path.endswith(".jsonl")):
            raise ValueError(
                f"append 的第一个参数必须是流水文件路径（.jsonl），收到 path={path!r}。"
                "看起来你把账的地址传成路径了。")
        if verdict is not None and (type(verdict) is not int or verdict not in (0, 1)):
            raise ValueError(
                f"verdict 只能是 1/0/None，收到 {verdict!r}（字符串一律不收：'对'/'错' 不是判决）。")
        if verdict is None:
            p = self.open_pending(key, why=why, need=reason, ctx_fp=ctx_fp, writer=writer)
            line = {"t": "pending", "key": key, "p": p.to_dict()}
        else:
            node = self.record(key, verdict, reason=reason, ctx_fp=ctx_fp,
                               value_score=value_score, ttl=ttl, writer=writer,
                               cross_fp=cross_fp)
            # 圈号不在这里定：追加的一方手上只有今天这一节，它不知道自己是第几圈。
            # 写 None，回放时按顺序补 —— 圈号属于"重演出来的位置"，不属于写的那一方。
            node = dict(node, round=None)
            line = {"t": "node", "key": key, "node": node}
        try:
            before = os.path.getsize(path)      # 我写之前，这代流水有多少字节
        except OSError:
            before = 0
        self._write_line(path, line)
        # 消费位置只推到"我写之前"那一位，不推到文件末尾 ——
        # 推到末尾会把**别人在这之前已经提交、我还没读到的行**一起标成"已消费"，
        # 那些行从此读不到（压实之后也还在备份里躺着，就是读不出来）。
        # 自己这一行留给下一次 _absorb 重演；重演按内容认人，不会记两遍。
        # 位置**一点都不推**：我写之前别人可能又追加了行，我却还没读到。
        # 一推就等于替别人签收，那些行从此读不到。留着旧位置，下次 _absorb 把它
        # 和这一行一起重演回来 —— _replay_node 按内容认人，同一节不会记两遍。
        _ = before
        return line

    _TLOCKS = {}
    _TLOCKS_GUARD = threading.Lock()
    _HELD = set()                    # 本进程此刻真握着的锁（区分"我的残渣"和"我的活锁"）

    @classmethod
    def _tlock(cls, path):
        """同一本流水的进程内互斥（可重入）：跨进程靠 .lock 文件，进程内靠这把。"""
        with cls._TLOCKS_GUARD:
            lk = cls._TLOCKS.get(path)
            if lk is None:
                lk = threading.RLock()
                cls._TLOCKS[path] = lk
            return lk

    @staticmethod
    def _lock_dead_hint(path):
        """这把锁是不是"已经没人握着"的料？（空锁 / 读不出 pid / pid 已死）

        只用来决定"要不要为它多等几秒"，不参与最终接管判断 —— 最终判断在 _lock 里。
        """
        try:
            raw = open(path, encoding="utf-8", errors="ignore").read().strip()
        except OSError:
            return False
        try:
            pid = int(raw.split()[0])
        except (ValueError, IndexError):
            return True                      # 空锁 / 垃圾锁
        return not Ledger._alive(pid)

    @staticmethod
    def _lock_own(path, stale_after=45):
        """拿锁，并且**立刻接管死锁**。

        锁里记着 pid：
        - 记的是我自己（同一进程里压实与写者共锁）：这是自己的残渣，清掉重来。
        - 记的那个 pid **已经死了**：主人不在了，锁就是废纸，当场接管 ——
          不能陪它等到 mtime 过期。以前这里只看 mtime（30 秒），而等锁的人只等 12 秒，
          于是"写者 unlink 失败留下的残锁"会让后面每个写者白等 12 秒再抛（外部评审A第七轮实测 2%）。
        - 记的 pid 还活着：让路，一个字节都不碰。
        """
        for _ in range(5):
            fd = Ledger._lock(path, stale_after=stale_after)
            if fd is not None:
                Ledger._HELD.add(path)
                return fd
            try:
                who = open(path, encoding="utf-8", errors="ignore").read().strip()
            except OSError:
                continue
            if not who:
                continue
            try:
                pid = int(who.split()[0])
            except (ValueError, IndexError):
                pid = None
            mine_now = pid == os.getpid() and path not in Ledger._HELD
            if pid is not None and (mine_now or not Ledger._alive(pid)):
                # 我自己留的残渣（记在我名下、但此刻我并没有握着它）或主人已死：当场接管
                Ledger._drop_lock(path)
                continue
            return None
        return None

    @staticmethod
    def _drop_lock(path, tries=10):
        Ledger._HELD.discard(path)
        """删锁要重试：Windows 上别人正好开着这个文件读 pid 时，unlink 会失败。
        删不掉就留在盘上 —— 但下一轮 _lock_own 认得它是死锁，会接管。"""
        for _ in range(tries):
            try:
                os.unlink(path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                time.sleep(0.02)
        return False

    @staticmethod
    def _write_line(path, line):
        """一行一次 write，不 flush 成半行 —— 追加是这本账唯一的并发安全姿势。

        Windows 上另一个人正在把这份流水改名（压实）时，open 会抛 PermissionError。
        以前这里不重试：写者当场抛错、线程死掉，那一轮判决就没了 ——
        不是账被改坏，是**写的人被打断了**，而账面看不出来（外部评审A第三轮实测 14/30）。
        压实改名只要几十毫秒，等一等就过去了；等不到才如实抛。
        """
        data = json.dumps(line, ensure_ascii=False) + "\n"
        blob = data.encode("utf-8")
        # 压实和写者共用这把锁：压实在拷底 + 截空的这一刻，没有写者能插进来。
        # 不共用的话，"截断"和"追加"会撞在一起 —— 行落在被截掉的那半里，谁也不知道。
        # 两个写者不能同时进；同一进程里再进来也不能自己把自己锁死（压实时会借这条）。
        with Ledger._tlock(path):
            fd = None
            wlock = path + ".lock"
            for _ in range(3000):                 # 护栏·非窗：并发锁等待上限（死锁会被当场接管），与判决 ttl 无关，别抄进 ttl
                fd = Ledger._lock_own(wlock)
                if fd is not None:
                    break
                time.sleep(0.02)
            if fd is None:
                raise OSError(f"流水被压实锁着，等不到：{wlock}")
            try:
                Ledger._write_line_unlocked(path, data, blob)
                return line
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                Ledger._drop_lock(wlock)

    @staticmethod
    def _heal_torn_tail(path):
        """把上一个写者**死在半路**留下的半行残尾截掉（拿锁之后、追加之前调进来）。

        为什么非在写这一侧治不可（崩溃注入实测出来的根因）：读的人只在换行处切
        （`_absorb` 的 `rfind`），残尾本身是惰的、进不了账 —— 但下一次追加是
        `open(path, "a")`，整行会**粘在残尾后面**，切出来的是「半行 + 新行」的杂交，
        `json.loads` 当场失败：坏一行、丢一行 —— 那一节从此世界上发生过、账上没发生
        （实测：崩在 write 中间之后再 append，`bad_lines` 当场 +1，新写的那节整节没进链）。

        口径：
          · 正常尾巴只花一次 1 字节的读，没病不花钱；
          · 真出事了才从后往前找最后一个换行，把残尾截到那儿（整份都没换行 =
            全截掉 —— 那是第一节就崩了，没有任何一行是完整的）；
          · 截不动就重试，重试不动**如实抛**，不许带着残尾往下写（写了就是坏一行丢一行）；
          · 位置语义不受影响：`_journal_upto` 永远停在某个换行上，残尾在它后面，
            截掉它谁都不丢已读的位置；真有人的偏移比截后的文件还大，
            `_absorb` 本来就会「缩水 = 换代，从头重读」，重演是幂等的。

        必须在锁里调：截断与追加之间不许插进第二个人（`_write_line` 持 tlock +
        `.lock` 调进来，压实的截空也共用同一把锁）。
        """
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return False                     # 这份流水还不存在：没有尾巴可治
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return False
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) == b"\n":
                return False                 # 正常尾巴：走人（这就是常规写路的全部开销）
            cut, pos, tail = 0, size, b""
            while pos > 0:                   # 从后往前找最后一个换行（只在出事时读整段）
                n = min(65536, pos)
                pos -= n
                os.lseek(fd, pos, os.SEEK_SET)
                tail = os.read(fd, n) + tail
                i = tail.rfind(b"\n")
                if i >= 0:
                    cut = pos + i + 1
                    break
        finally:
            os.close(fd)
        for _ in range(40):                  # 截断要跟别人错开（Windows 上有人正开着读）
            try:
                with open(path, "r+b") as f:
                    f.truncate(cut)
                    f.flush()
                    os.fsync(f.fileno())
                return True
            except OSError:
                time.sleep(0.02)
        raise OSError(f"半行残尾截不掉（重试 40 次仍未落稳）：{path}")

    @staticmethod
    def _write_line_unlocked(path, data, blob):
        # 先治上一个写者留下的半行残尾，再追加（详见 _heal_torn_tail 的说明）。
        Ledger._heal_torn_tail(path)
        for i in range(40):
            try:
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(data)
                    f.flush()
                    end = f.tell()
                    # 我写的这一代文件是哪一代：后面要拿它跟"现在路径上是谁"比。
                    # 只比"路径上能不能读到这一行"不够 —— 压实是我 open 之后、写完之前
                    # 把流水改名走的，那一行落在旧代（备份）里，路径上迟早也有这一行
                    # （别的写者重写同一批），读得到，但**不是我这一行**。
                    mine = os.fstat(f.fileno()).st_ino
                # 写完要**回头看这份文件还在不在**：压实可能在我 open 之后把流水改名走，
                # 我这一行就落进了备份里，而新流水里没有它 —— 读的人只看新流水，
                # 这行就永远读不到（账上没发生，世界上发生了）。
                # 怎么确认"这一行现在就在 path 上"：把文件尾巴读出来找它。
                # 只比大小不够 —— 新流水可能已经比我的 end 更大，一比就以为写对了，
                # 其实我这行在上一代（已改名的备份）里。读到本行才算数，读不到就重写。
                if os.stat(path).st_ino == mine:
                    with open(path, "rb") as f:
                        f.seek(max(0, end - len(blob)))
                        if blob in f.read():
                            return True
                # 路径上换了一代（压实把我这代改名走了）：这一行在旧代里，
                # 读的人只看新代 —— 重走去写新代。
            except OSError:
                # 不只是 PermissionError：压实改名的那一瞬，路径可能**根本不存在**
                # （旧名已走、新名还没建），Windows 抛的是 FileNotFoundError。
                # 只接 PermissionError 的话，写者在这里当场死掉，后面几条全没了。
                if i == 39:
                    raise
            time.sleep(0.02)
        # 40 次都没能确认"这一行现在就在路径上"：如实抛，不假装写成功。
        # 以前这里悄悄走出函数、返回 None —— 调用方以为记上了，账面却少一节。
        raise OSError(f"写不进去（重试 40 次仍未确认落稳）：{path}")

    def backfill_value(self, key, rnd, value_score):
        """事后回填：真被用上了、真拦下了，才给分。"""
        for n in self.nodes.get(key, []):
            if n["round"] == rnd:
                n["value_score"] = value_score
                return n
        return None

    # ══════════ 六个取用面（同一节，六个侧面） ══════════
    @staticmethod
    def is_tombstone(node):
        """被遗忘的一节留下的位子：不占格，但圈号不许乱。"""
        return node.get("verdict") is None and bool(node.get("tombstone"))

    def _last_real(self, chain):
        for n in reversed(chain or []):
            if not self.is_tombstone(n):
                return n
        return None

    def by_value(self, key):
        for n in reversed(self.nodes.get(key, [])):
            if not self.is_tombstone(n) and n["value_score"] is not None:
                return n["value_score"]
        return None

    def by_verdict(self, key):
        n = self._last_real(self.nodes.get(key))
        return n["verdict"] if n else None

    def by_cache(self, key):
        n = self._last_real(self.nodes.get(key))
        return (n["hits"], n["last_hit"]) if n else None

    def by_writer(self, key=None):
        """按写者分组：这本账是谁跟谁说过的，一眼看清。

        署名存了就得有读法 —— 否则想读只能伸手进 nodes[key][i]["writer"]，
        那正是我们自己批评过的"私货"反模式。加读取接口不算加面。
        """
        out = {}
        items = [(key, self.nodes[key])] if key is not None else self.nodes.items()
        for k, c in items:
            for n in c:
                if self.is_tombstone(n):
                    continue
                out.setdefault(n.get("writer") or "", []).append(n)
        return out

    def by_round(self, key):
        c = self.nodes.get(key)
        return [n["round"] for n in c] if c else None

    def by_time(self, key):
        n = self._last_real(self.nodes.get(key))
        return (n["ts"], n["last_hit"]) if n else None

    def recent(self, n):
        """最近 N 节 —— **整本账**的时间线（跨地址），只读、不改链、不落一个字节。

        三件口径（说不清就不开这个口）：

        ① 查什么 —— 所有地址上的每一节，节点自带 `key`，穿成一条时间线。
        ② 按什么排 —— 按**节自带的钟** `ts` 降序：链序只在单地址里有意义，
           跨地址没有共同的链序（两个地址各转各的圈，圈号 1 不比圈号 3 旧）。
           同一个 `ts` 按圈号降序、再按地址升序 —— 次序是确定的，不看字典的迭代顺序。
        ③ 边界给谁 —— **墓碑不算**（「忘」那一档判决栏已空，它不在时间线上）；
           **褪色节算**（`prune` 褪成梗概的那档判决和圈号还留着，它还在这本账上）；
           n 从最新往回数 n 节，不够就全给；`n <= 0` 给 `[]`；没有钟的节排不了，
           照 `sweep_ttl` 的规矩跳过（别拿 0 当钟）。

        面还是六个 —— 这不是第七个面，是把六个面按时间穿起来换个姿势看。
        """
        if type(n) is not int:
            raise ValueError(f"recent(n) 要一个整数节，收到 {n!r} —— 要几节就报几节。")
        if n <= 0:
            return []
        out = []
        for chain in self.nodes.values():
            for node in chain or ():
                if self.is_tombstone(node):
                    continue                    # ③ 忘了的不在时间线上
                if not isinstance(node.get("ts"), (int, float)):
                    continue                    # 没钟的排不了位（同 sweep_ttl：别拿 0 当钟）
                out.append(node)
        out.sort(key=lambda m: (-m["ts"], -(m.get("round") or 0), m.get("key") or ""))
        return out[:n]

    def by_pair(self, key, ctx_fp):
        """⑥ 配得上才叫可借

        两边都没给指纹 = 同一个默认组，算配上（以前要求两边都非空，
        于是"不传指纹"的人拿到的是一本只写不读的账：记了、永远借不到、每次全重算）。
        """
        want = ctx_fp or ""
        for n in reversed(self.nodes.get(key, [])):
            if (n.get("ctx_fp") or "") == want:
                return n
        return None

    def by_pending(self, key):
        """判决栏旁边那个位置：还在走的，差什么、第几圈、试过几次"""
        return self.open.get(key)

    def six_port(self, key, ctx_fp="", rnd=None):
        """六个取用面：**同一节**的六个侧面 —— 同一个圈号锚住，不拿两节拼一份。

        rnd=None 锚"最新一节"（跳过墓碑）；要看第 3 圈就 rnd=3。
        以前这六个口各取各的：①找"最后一个有分的节"、②③⑤取"最新节"、④给整串圈号——
        于是一条地址判过两圈、只有第 1 圈有分时，六面齐满却是两节拼的
        （2026-10-31 两份外部评审同时抓到这里）。六个面必须能拼回**一节**账。
        """
        chain = self.nodes.get(key) or []
        if rnd is not None:
            node = next((n for n in chain if n.get("round") == rnd), None)
        else:
            node = self._last_real(chain)
        if node is None:
            return {"①值": None, "②判决": None, "③命中": None, "④圈": None,
                    "⑤时间": None, "⑥配对": False}, 0
        got = {
            "①值": node.get("value_score"),
            "②判决": node.get("verdict"),
            "③命中": (node.get("hits"), node.get("last_hit")),
            "④圈": node.get("round"),
            "⑤时间": (node.get("ts"), node.get("last_hit")),
            "⑥配对": bool(node.get("ctx_fp") and ctx_fp and node["ctx_fp"] == ctx_fp),
        }
        # 判决为 0 也是判过（0 ≠ 没判）；命中 (0, None) 是"记过但没人翻"，算有数
        filled = sum([
            got["①值"] is not None,
            got["②判决"] is not None,
            got["③命中"] is not None,
            got["④圈"] is not None,
            got["⑤时间"][0] is not None,
            got["⑥配对"],
        ])
        return got, filled

    # ══════════ 借：配得上、且没过窗，才准抄 ══════════
    def _window_ok(self, node, now):
        """这一节自己的钟优先；账本级的 ttl 只是"从哪来"，不是"一刀切"。

        一本账里同时住着两分钟一圈的器官和一年一圈的保险 ——
        共用一个 ttl，等于拿肠子的节奏管神经元。
        """
        window = node.get("ttl")
        if window is None:
            window = self.ttl
        if window is None:
            raise LedgerBlocked(
                "这一节没有钟：record(ttl=...) 把窗写进本节，或 Ledger(ttl=...) 给个来源，二选一。"
            )
        ttl = window(node) if callable(window) else window
        if ttl is None:
            return False
        if ttl <= 0:
            # callable 只有在运行时才算得出窗；算出来是非正数，同样按"没有钟"处理。
            raise LedgerBlocked(f"窗算出来是 {ttl!r}：窗必须是正数")
        # 边界：账龄正好等于 ttl 算窗内（契约 3.1）。
        return (now - node["ts"]) <= ttl

    def probe(self, key, ctx_fp="", checked=False, min_age=None, *, journal, writer=None):
        """借一节。借到返回那一节，借不到返回 None —— 没借到的原因记在 miss 里。

        checked : 抄的时候你到底核没核。这句必须由你声明，账上会留痕。
        min_age : 太新也不借。刚判完三秒又问一遍，那是抖动，不是结论。不传 = 不管。
        journal : **必填，没有默认值。**
                  给流水路径 = 这一借落成一行，多进程谁也不盖谁；
                  给 False   = 明说"这次不落账"（只许单进程、只许当查询用 ——
                               多进程会互相盖掉 hits，而 prune() 正靠 hits>0
                               决定"别忘了这节"：借得越多、忘得越快）。
                  不给       = 报错。默认"不落账"就是在教人踩那个坑。
        """
        if journal is None or journal == "":
            raise ValueError(
                "借票必须落账：probe(..., journal=<流水路径>)；"
                "确实不要落账就明写 journal=False（只许单进程）")
        self.stat["req"] += 1
        now = self._clock()
        if key not in self.nodes:
            self.miss[key] = NO_RECORD
            return None
        node = self.by_pair(key, ctx_fp)
        if node is None:
            self.miss[key] = MISMATCH       # 有账，配不上 —— 这不是空，是"该重算"
            return None
        if min_age is not None and (now - node["ts"]) < min_age:
            self.miss[key] = TOO_FRESH
            return None
        if not self._window_ok(node, now):
            self.miss[key] = EXPIRED
            return None
        if journal is not False:
            # 借这一下也是一件事，进流水：只追加，多进程谁也不盖谁。
            # 写完立刻从消费位置重演一次 —— 自己写的那行也走同一条路回放，
            # 不去手工改内存（手工改 + 回放 = 算两遍）。
            self._write_line(journal, {"t": "hit", "key": key, "round": node["round"],
                                       "ts": now, "checked": bool(checked),
                                       "hid": uuid.uuid4().hex,
                                       "by": self._who(writer)})
            self._journal_upto = self._absorb(journal, self._journal_upto)
        else:
            node["hits"] += 1
            node["last_hit"] = now
            node["borrows"].append({"from_round": node["round"], "checked": bool(checked),
                                    "ts": now})
        self.miss.pop(key, None)
        self.stat["borrowed"] += 1
        return node

    def why_not(self, key):
        """刚才为什么没借到：no_record / expired / mismatch"""
        return self.miss.get(key)

    # ══════════ 账的分析 ══════════
    @staticmethod
    def antipode(v):
        """对面：判 1 就是判了"不是 0"。

        对面不需要定义，它跟判决同一刻成立 —— 0 和 1 是同一件事的两个面。
        所以这里没有"翻面规则"可配：想改它，等于想把互补项改成不是互补项。
        """
        if v not in (0, 1):
            raise ValueError("空没有对面：那一对还没结成")
        return 1 - v

    def flips(self, key=None):
        """对面显形：同一个地址，相邻两圈落下的判决不同。

        它不是"我们规定这算一次翻面"，而是 0/1 互补关系在时间上的投影 ——
        第二次观测把第一次那节账的对面照出来了。没有 round，就永远看不见。
        """
        out = []
        items = [(key, self.nodes[key])] if key is not None else self.nodes.items()
        for k, c in items:
            for a, b in zip(c, c[1:]):
                if self.is_tombstone(a) or self.is_tombstone(b):
                    continue                      # 忘了的不算翻面
                if a["verdict"] != b["verdict"]:
                    out.append({"key": k, "from": a["verdict"], "to": b["verdict"],
                                "round": f"{a['round']}→{b['round']}", "ts": b["ts"],
                                "why": b["reason"],
                                # 两种翻面必须分开：世界动了，还是我们动了
                                "kind": "判据变了" if b.get("revisit_of") else "情况变了",
                                "mirror_ts": b.get("mirror_ts")})
        return out

    def cold(self):
        """冷账：记下来一次没被翻过。账本被养爆的元凶"""
        return [n for c in self.nodes.values() for n in c
                if not self.is_tombstone(n) and n["hits"] == 0]

    def prune(self, key=None, keep_last=1):
        """三档遗忘（设计决定 2026-11-04 —— 记忆是渐暗的，不是灯开关）：

        · 鲜活（全记）：被翻过的（hits > 0）、在翻转序列上的、revisit 过的、
          打过分的（value_score 有值 ＝ 印象深刻）、最后 keep_last 圈（当前判决）。
        · 梗概（大概记一点）：判过、没人翻、没分、不在翻转上的平淡前圈 ——
          判决和圈号留着（「我判过它是 1」），情形指纹、借账这些细节褪掉。
        · 忘（墓碑）：只由按钟那把刀（sweep_ttl）动；已有的墓碑不回改。

        人不是长成一个人的，是死成一个的；但大多数人大多数事，只记得个大概。
        """
        targets = [key] if key is not None else list(self.nodes.keys())
        killed = 0          # 语义已变：这把刀现在只降级，不再立墓碑
        for k in targets:
            chain = self.nodes.get(k, [])
            keep = set()
            for i, n in enumerate(chain):
                if self.is_tombstone(n):
                    keep.add(i)
                    continue
                if n["hits"] > 0 or n.get("revisit_of") or n.get("value_score") is not None:
                    keep.add(i)      # 印象深刻（打过分的）也全记 —— 分是回填对过答案的证据
                for sib in (chain[i - 1] if i else None,
                            chain[i + 1] if i + 1 < len(chain) else None):
                    if (sib is not None and not self.is_tombstone(sib)
                            and sib["verdict"] != n["verdict"]):
                        keep.add(i)
            for i in range(max(0, len(chain) - keep_last), len(chain)):
                keep.add(i)
            for i, n in enumerate(chain):
                if i in keep or self.is_tombstone(n):
                    continue
                if n.get("demoted"):
                    continue         # 已经是梗概了：褪一次就够，不重复计
                # 梗概（大概记一点）：判决、圈号、时间留住；情形指纹、借账细节褪掉。
                # cross_fp 键直接不写（跟墓碑同款：没键就不算"有键空着"）。
                chain[i] = {
                    "nid": n.get("nid"), "key": k,
                    "verdict": n.get("verdict"),          # ← 记得判过什么，这是梗概的底线
                    "reason": "[记不全·细节已褪]",
                    "ts": n.get("ts"), "round": n.get("round"), "value_score": None,
                    "ctx_fp": "", "hits": n.get("hits", 0), "last_hit": n.get("last_hit"),
                    "borrows": [], "ttl": None,
                    "writer": n.get("writer"),            # 署名不丢：不署名不许落账
                    "demoted": True,                      # 这是梗概，不是墓碑
                }
                killed += 1
        self.stat["demoted"] = self.stat.get("demoted", 0) + killed
        return killed

    # ══════════ 第三把刀：按钟忘（2026-11-03）══════════
    # prune() 按"判过没人翻"忘，判据与 ttl 无关 —— 一本每节只判一圈的账跑一次 0 刀。
    # 可是 ttl 全是 3600、节节早出窗，账面 124/129 超期：缺的是这把按钟扫的。
    # 归档位置一个默认值都不给（通用件不许写死业务路径）：不给 archive 就不动刀。

    def sweep_ttl(self, now=None, archive=None):
        """过窗的节：归档在前，再原地变墓碑（与 prune() 同口径，圈号一个字不许乱）。

        归档：原节完整 JSON 逐行追加进 archive（只追加不覆盖）——
        先留底再改，改没了就再也找不回原样了。**不给归档位置就不动刀**：
        留不下底就不写，返回 0（一个默认值都不给）；真扫出过窗的节才打印
        一句吵给日志看 —— 没目标本来就是 0 刀，无话可说。
        墓碑：字段完全复用 prune() 那套（nid/key/round 留着、verdict=None、ttl=None），
        reason 写"已过窗（ttl 超期）"。不整条抽走：抽走会把后面的圈号往前挪。

        绝不碰 journal.jsonl（追加型正本，宿主靠 _journal_upto/_journal_ino 消费它）。
        **绝不抛**：不给 archive → 返回 0；跑起来出的错同样 fail-open ——
        吞掉打印返回 0。调用方 record 把这把刀夹在 append 和 save 中间的前提就是
        「内部 fail-open，绝不抛」；参数错只能吵给日志看，不能炸在 try 外面。
        """
        try:
            now = self._clock() if now is None else now
            targets = []
            for k, chain in self.nodes.items():
                for i, n in enumerate(chain):
                    if self.is_tombstone(n):
                        continue                    # 已经是墓碑：不重复扫
                    w = n.get("ttl")
                    if w is None:
                        continue                    # 没钟的不判（判不了 ≠ 已过期）
                    if callable(w):                 # 运行时才算的窗：算得出来才扫
                        try:
                            w = w(n)
                        except Exception:
                            continue
                        if not isinstance(w, (int, float)) or w <= 0:
                            continue
                    ts = n.get("ts")
                    if not isinstance(ts, (int, float)):
                        continue                    # 没时间戳的不判，别拿 0 当钟
                    if (now - ts) > w:
                        targets.append((k, i, n))
            if not targets:
                return 0
            if not archive:
                # 归档位置一个默认值都不给 → 不动刀。打印走 stderr：stdio 的
                # MCP 子进程把 stdout 当 JSON-RPC 通道，混进一行就是
                # "Expecting value"（自测实测）；且只在真有过窗节时吵 ——
                # 无目标 = 0 刀，没什么可吵的。
                print("[sweep_ttl] 没给归档位置（archive=<.jsonl 路径>）：先归档后动刀，"
                      "留不下底就不写 —— 这一刀不扫，调用不断。", file=sys.stderr)
                return 0
            # ① 归档在前：一节一行完整原样，只追加不覆盖。
            arc = archive
            d = os.path.dirname(arc)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(arc, "a", encoding="utf-8") as f:
                for _, _, n in targets:
                    f.write(json.dumps(n, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # ② 再原地变墓碑：原地占位，圈号不动、nid 不丢（重演靠 nid 认人）。
            swept = 0
            for k, i, n in targets:
                chain = self.nodes.get(k) or []
                if i >= len(chain) or chain[i] is not n or self.is_tombstone(n):
                    continue                        # 这一节刚被动过：让位，不硬改
                chain[i] = {
                    "nid": n.get("nid"),
                    "key": k, "verdict": None, "tombstone": n.get("round"),
                    "reason": "已过窗（ttl 超期）",
                    "ts": n.get("ts"), "round": n.get("round"),
                    "value_score": None, "ctx_fp": "", "hits": 0,
                    "last_hit": None, "borrows": [], "ttl": None,
                    "writer": n.get("writer"),        # 署名不丢：不署名不许落账
                }
                swept += 1
            if swept:
                self.stat["swept"] = self.stat.get("swept", 0) + swept
            return swept
        except Exception as e:
            print(f"[sweep_ttl] 失败（fail-open，主流程不断）：{type(e).__name__}: {e}")
            return 0

    def size(self):
        """还活着的节（墓碑不算格，但圈号还占着）"""
        return sum(1 for c in self.nodes.values() for n in c if not self.is_tombstone(n))

    def tombstones(self):
        return sum(1 for c in self.nodes.values() for n in c if self.is_tombstone(n))

    def keys(self):
        return len(self.nodes)

    def unresolved(self):
        """还悬着的：正在进行中的一共有几条"""
        return list(self.open.values())

    # ══════════ 落盘：不落盘的账本，重启一次就归零 ══════════
    def to_dict(self):
        # unresolved 就是"现在还有几笔悬着"，从 open 现数 —— 存一个会走样的计数器，
        # 迟早出现"悬位是空的、计数还是 1"（评审实测 2026-10-31 抓到过）。
        st = dict(self.stat)
        st["unresolved"] = len(self.open)
        st.setdefault("opened", st.get("unresolved", 0))
        return {
            "version": 1,
            # 流水正本读到哪个字节了 —— 快照只是压实产物，重演要接着上次的位置读，
            # 不然同一节会被重演两遍（外部评审A 2026-10-31 实测：save(compact_journal=False) 后节点翻倍）。
            "_journal_upto": int(getattr(self, "_journal_upto", 0)),
            # 位置是"对着哪一个文件"数的：不带文件身份，压实换了代、新代又恰好更长时，
            # 新代开头几行会被当成"已经读过"跳过 —— 静默丢一节（2026-10-31 整本连跑 0.3% 复现）。
            "_journal_ino": getattr(self, "_journal_ino", None),
            "compacted": list(getattr(self, "_compacted", [])),
            "sealed": dict(getattr(self, "_sealed", {})),
            "writer": self.writer,      # 这本账是谁的（不是我猜的，是它写的）
            "nodes": self.nodes,
            "open": {k: p.to_dict() for k, p in self.open.items()},
            "stat": st,
        }

    def save(self, path, compact_journal=False, journal=None):
        """写快照 —— 原子写，且先把流水里别人刚写的行吸收进来，**绝不吃账**。

        compact_journal 默认 False：**流水才是正本，快照只是压实产物**。
        以前这里默认清流水，实测会把短命进程刚 append 的那行连同快照一起吃掉
        （2026-10-31 体检第 14 项复现）。要压实请显式调 compact()：
        它把流水原子改名留底，不删、不清。
        """
        if compact_journal:
            raise RuntimeError(
                "save() 不再清流水：清流水会吃掉别的进程刚写的行。"
                "要压实请用 compact(path)（流水会改名留底，不丢）")
        # 流水名由调用方定（journal=），默认才是 path + ".jsonl"。
        # 以前这里写死 path+".jsonl"：短命进程把流水叫 judgments.jsonl、常驻进程按默认名去读，
        # 读到的是空、快照里 0 节、全程不报错 —— 一周后账面全空，还查不出是哪一步丢的。
        jp = journal if journal is not None else (path + ".jsonl")
        if os.path.exists(jp):
            self._journal_upto = self._absorb(jp, getattr(self, "_journal_upto", 0))
        if os.path.exists(path):
            stamp = (os.path.getmtime(path), os.path.getsize(path))
            if stamp != getattr(self, "_stamp", None):
                # 我读进来之后，硬盘上这本被别人改过了：先合并再写，
                # 否则就是拿旧账盖新账（经典的丢更新）。
                self._merge_from(type(self).load(path, ttl=self.ttl, deliver=self.deliver,
                                                 writer=self.writer, journal=jp))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        # 原子：读的人要么看到旧本，要么看到新本，没有半本。
        # Windows 上另有人在读这份快照时，改名会 WinError 5/32 —— 等一下再来，
        # 等不到就明说，不许装作存上了。
        for _ in range(40):
            try:
                os.replace(tmp, path)
                self._stamp = (os.path.getmtime(path), os.path.getsize(path))
                return path
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, path)          # 还不行就让它自己把错报出来
        return path

    def _merge_from(self, other):
        """把别人账上的并进来：同圈同内容的不重记，别人的悬位也不丢。

        合并只在"两边都改了同一本账"时发生（压实对齐 / 存盘前发现磁盘变了）。
        圈号撞了（同号不同内容）才按时间重排一次 —— 否则谁都别动谁的号。
        """
        for k, chain in getattr(other, "nodes", {}).items():
            mine = self.nodes.setdefault(k, [])
            fresh = []
            for node in chain:
                if not any(self._same_node(m, node) for m in mine):
                    mine.append(node)
                    fresh.append(id(node))
            # 撞号怎么办：**盘上（other）那几节是锚，号一个字不动**；
            # 我这边还没落盘的，只要有一节撞上锚，就**整批**按时间排好、连号顺延到锚后面。
            # 为什么必须整批：以前只给撞号那一节顺延，没撞的那节留在原位 ——
            # 冷读出来 (1,B-1)(2,B-2)(3,A-3)(4,A-1)(5,A-2)，链上时间倒流，
            # flips 多报一次"没发生过的改判"、probe 交回的不是最新判决（两位评审实测）。
            # 反过来按"整条链 ts 重排"也不行：盘上的号会被改（时钟不准时尤其明显）。
            # 锚定盘上 + 撞进来的整批顺延，两条都保住。
            anchored = set()
            for node in chain:
                for m in mine:
                    if self._same_node(m, node):
                        anchored.add(id(m))
                        break
            anchored_rounds = {n.get("round") for n in mine if id(n) in anchored}
            collided = any(id(n) not in anchored and n.get("round") in anchored_rounds
                           for n in mine)
            if collided:
                nxt = max([r for r in anchored_rounds if r] + [0]) + 1
                for n in sorted((n for n in mine if id(n) not in anchored),
                                key=lambda n: n.get("ts") or 0):
                    n["round"] = nxt
                    nxt += 1
                    self.stat["round_conflict"] = self.stat.get("round_conflict", 0) + 1
            mine.sort(key=lambda n: n.get("round") or 0)
        for k, p in getattr(other, "open", {}).items():
            self.open.setdefault(k, p)
        self._journal_upto = max(getattr(self, "_journal_upto", 0) or 0,
                                 getattr(other, "_journal_upto", 0) or 0)
        self._compacted = list(dict.fromkeys(
            list(getattr(self, "_compacted", []) or []) +
            list(getattr(other, "_compacted", []) or [])))
        # 消费位置取两者里更靠后的那个：谁读得多以谁为准（读多的那个已经把那几行重演过了，
        # 重演是幂等的，所以取 max 只会少读、不会少记）
        mine = dict(getattr(self, "_sealed", {}) or {})
        for name, off in (getattr(other, "_sealed", {}) or {}).items():
            mine[name] = max(int(mine.get(name, 0) or 0), int(off or 0))
        self._sealed = mine

    def compact(self, path, journal=None):
        """压实：吸收流水 → 写快照 → 改名留底 → **把改名窗口里撞进来的行补回来**。

        窗口是真的（2026-10-31 外部评审A评审指出）：吸收读到 EOF 之后、改名之前，
        任何一次 append 都落进正要被改名走的那个文件。这里从改名后的文件里
        接着上次消费的位置继续读 —— 压实时撞进来的行也进账，不算丢。

        两个压实者同时跑会各自把对方的纸抽走，所以这条路**带锁**：
        拿不到锁就让这一次，直接返回 False —— 账不会丢，行还在流水里等下一轮。
        烂锁按「主人还在不在」判，不按老不老判：pid 已死当场接管、空锁 5 秒接管、
        mtime 超过 45 秒（持锁者每 15 秒续命）也接管。
        """
        lock = path + ".compact.lock"
        # 上一轮压完"删锁失败"留下的锁是**我的残渣**，不是活人的锁：
        # 只有我自己知道那是我留的（记在 _left_lock 里），所以只在我这一侧清理，
        # 别人照旧让路。以前一律判活让路 —— 这个进程从第一次删锁失败起就一辈子压不了实。
        if getattr(self, "_left_lock", None) == lock and os.path.exists(lock):
            Ledger._drop_lock(lock)
        else:
            self._left_lock = None
        # 用 _lock_own：活人的锁照旧让路，主人已经死了的锁当场接管（不陪它等 mtime）
        fd = Ledger._lock_own(lock)
        if fd is None and Ledger._lock_dead_hint(lock):
            # 这把锁看着已经没人握着了（空锁要到 5 秒、假活锁要到 45 秒才够接管期）。
            # 如果这里直接让路返回 False，而场上又**没有写者**来踩它，这把垃圾锁就没人清、
            # 压实也永远压不动（外部评审B第九轮实测：空锁时 compact 0.0s 返回 False、锁留在盘上）。
            # 压实是后台活，为它多等几秒不吃亏 —— 等到接管的钟点，接管它。
            for _ in range(50):
                time.sleep(0.2)
                fd = Ledger._lock_own(lock)
                if fd is not None:
                    break
        if fd is None:
            # 三个返回值必须分得清（外部评审A第二轮：全是 falsy 就看不出压实成功过几次）：
            #   <备份路径> = 压成了    None = 本来就没流水    False = 有人在压，这次让路
            self.stat["compact_skipped"] = self.stat.get("compact_skipped", 0) + 1
            return False
        # 压实的"拷底 + 截空"必须跟写者串行：进程内 RLock + 跨进程 .lock，两把都要。
        jp_path = journal if journal is not None else (path + ".jsonl")
        holder = Ledger._tlock(jp_path)
        holder.acquire()
        wlock = jp_path + ".lock"
        wfd = None
        for _ in range(300):
            wfd = Ledger._lock_own(wlock)
            if wfd is not None:
                break
            time.sleep(0.02)
        if wfd is None:
            # 拿不到写者的锁：这一轮不压。行还在流水里等下一轮，不丢。
            try:
                os.close(fd)
            except OSError:
                pass
            holder.release()
            self.stat["compact_skipped"] = self.stat.get("compact_skipped", 0) + 1
            return False
        stop_beat = threading.Event()
        # 两把锁都要续命：压实期间它同时握着"压实锁"和"写者锁"。只暖前者的话，
        # 一次超过 45 秒的大压实，写者锁会被别人按 mtime 兜底接管 ——  thief 在
        # 截空的当口插进来写，正好是最不该并发的那一刻。
        beat = threading.Thread(target=self._keep_lock_warm, args=((lock, wlock), stop_beat), daemon=True)
        beat.start()                                 # 压得久也不算"烂锁"（见 _lock）
        try:
            # 动手前先按磁盘对齐一次：手上这份可能比磁盘旧（别人刚压过、或自己
            # 还没 save）。拿旧的内存去盖新快照 = 把别人刚压进去的账盖掉。
            jp = journal if journal is not None else (path + ".jsonl")
            if os.path.exists(path):
                fresh = type(self).load(path, ttl=self.ttl, deliver=self.deliver,
                                        writer=self.writer, journal=jp)
                self._merge_from(fresh)      # 合并，不是覆盖：我还没落盘的那几节也得留着
            if os.path.exists(jp):
                self._journal_upto = self._absorb(jp, getattr(self, "_journal_upto", 0))
            self.save(path, journal=jp)
            if not os.path.exists(jp):
                return None
            conv = self._journal_upto                    # 已经吃进快照的字节数
            # 备份名带纳秒 + pid：毫秒级在同一毫秒压两次就会撞名，
            # 而 os.replace 撞名是**覆盖**不是并存 —— 被盖掉的那份里若还有没进快照的行，
            # 它就没了。这一次丢行的主因不是撞名（是写者被打断），但既然顺手，
            # 就别留着"同名覆盖"这个隐患。
            bak = f"{jp}.{time.time_ns()}.{os.getpid()}.compact"
            # **不换名，只拷贝 + 截断。** 换名会制造"两代文件"：写者手里那只旧句柄
            # 还能往已经改名的文件里写，读的人却只看新代；偏移是对着哪一代数的也说不清。
            # 整本连跑 0.3% 概率静默丢一节，根子都在这。
            # 现在写者与压实共用同一把 `.lock`：这一刻没有人在写，拷一份底、把正本截空，
            # 文件自始至终是同一个（inode 不变，偏移语义不变）。
            try:
                shutil.copyfile(jp, bak)
            except OSError:
                self.stat["compact_skipped"] = self.stat.get("compact_skipped", 0) + 1
                return False
            with open(jp, "w", encoding="utf-8", newline="") as f:
                f.truncate(0)
                f.flush()
                os.fsync(f.fileno())
            # 窗口里撞进来的行，一行不丢。写方可能在我读完那一瞬还在往**旧句柄**里写
            # （它 open 的时候 jp 还没改名，write 的时候已经叫 bak 了）—— 所以读到
            # 稳定为止再登记，否则"登记为已消费"会把尾巴一起藏掉。
            window = conv
            for _ in range(50):
                before = os.path.getsize(bak)
                window = self._absorb(bak, window)
                if os.path.getsize(bak) == before:
                    break
            self._sealed[os.path.basename(bak)] = os.path.getsize(bak)
            self._compacted.append(os.path.basename(bak))
            self._journal_upto = 0                       # 正本已截空：从头算
            # 文件没换（只截断）：inode 不变，位置语义不变，不用清 _journal_ino
            self.save(path, journal=jp)                  # 快照落定（含窗口补回的行与已吃名单）
            # 压完自己数一遍：备份里（从 conv 往后）的每一行，快照里都得有对应那一节。
            # 数不齐就再吸收再落盘，最多 5 轮。为什么要这层：写者可能在我"读到稳定"
            # 之后才把行写进已经改名的旧文件（写的时候 fd 还开着）——那一行进得了备份、
            # 进不了快照，账面就是"没发生过"。整本 36 项连跑时约 3% 概率踩到。
            for _ in range(5):
                if not self._unaccounted(bak, conv):
                    break
                window = self._absorb(bak, conv)
                self.save(path, journal=jp)
            else:
                self.stat["compact_lost"] = self.stat.get("compact_lost", 0) + 1
            self.stat["compacted"] = self.stat.get("compacted", 0) + 1
            return bak
        finally:
            stop_beat.set()
            beat.join(timeout=1)
            try:
                os.close(fd)
            except OSError:
                pass
            if wfd is not None:
                try:
                    os.close(wfd)
                except OSError:
                    pass
                Ledger._drop_lock(wlock)
            holder.release()
            try:
                # 删锁失败以前 except OSError: pass 吞掉 —— 锁里是本进程 pid（活人），
                # mtime 又被心跳续着，于是这个进程把自己永久锁死，一次也压不成。
                # 重试几次；还是不行就如实记一笔，别装没发生。
                Ledger._drop_lock(lock, tries=20)
                if os.path.exists(lock):
                    self._left_lock = lock          # 记下来：下一轮这是我的残渣，可以清
                    self.stat["lock_left"] = self.stat.get("lock_left", 0) + 1
            except OSError:
                pass

    @staticmethod
    def _lock(path, stale_after=45):
        """独占一把锁：拿到返回 fd，拿不到返回 None。

        烂锁要判**人还在不在**，不只判老不老：一本大账真压过 5 分钟，光看 mtime
        会把活锁当烂锁删掉（外部评审A第三轮指出：互斥会随账本长大自己失效）。
        锁里记着写它的 pid，pid 活着、锁也还在续命（每 15 秒）就不许抢；
        mtime 老过 45 秒说明没人握着它 —— pid 会被复用、进程退了句柄没释放时
        _alive 也会骗人，所以最后还得靠 mtime 兜底。
        """
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode())
                except OSError:
                    # pid 没写进去就会留下一个 0 字节锁：别人既查不到 pid，也就没人
                    # 满足"主人已死"的接管条件 —— 这把锁会永久留在那儿（外部评审A第八轮：66 秒后抛、永不自愈）。
                    # 写不进去就当没拿到：撤掉刚建的空锁，如实抛。
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    Ledger._drop_lock(path)
                    raise
                return fd
            except (FileExistsError, PermissionError):
                # Windows 上"有人正开着这个锁文件"（哪怕只是读）时，O_EXCL 建文件
                # 报的不是 FileExistsError 而是 PermissionError（删挂起时也这样）。
                # 只接 FileExistsError，PermissionError 会一路穿透到调用方 ——
                # 写者当场抛、整批判决没写（外部评审B第七轮 300 轮里 3 次，另有一次直接把脚本崩掉）。
                # 两种异常在这里含义一样：**这把锁现在不归你**。
                try:
                    age = time.time() - os.path.getmtime(path)
                    raw = open(path, encoding="utf-8", errors="ignore").read().strip()
                except OSError:
                    return None
                try:
                    pid = int(raw.split()[0])
                except (ValueError, IndexError):
                    pid = None
                if pid is not None and not Ledger._alive(pid):
                    Ledger._drop_lock(path)      # 主人已经没了
                    continue
                if pid is None and age > 5:
                    # 空锁 / 垃圾锁：正常持锁的人建完文件就立刻写 pid，5 秒还没写进来
                    # 说明写的人已经死在 create 与 write 之间 —— 不接管就永久堵死。
                    Ledger._drop_lock(path)
                    continue
                if age > stale_after:
                    # 最后的兜底：持锁的人每 15 秒给 mtime 续命，超过 stale_after 还没续，
                    # 说明没人握着它。光看 pid 不够 —— pid 会被系统复用给新进程
                    # （外部评审A第八轮实测 150 个短命进程里 23% 撞号），而且进程退出了句柄
                    # 还没释放时 _alive 仍报"活着"。这两种都让死锁看起来像活锁。
                    Ledger._drop_lock(path)
                    continue
                return None
        return None

    @staticmethod
    def _keep_lock_warm(paths, stop, every=15):
        """压实期间给手上的锁续命：mtime 一直新鲜，别人就不会当你死了。"""
        if isinstance(paths, str):
            paths = (paths,)
        while not stop.wait(every):
            for path in paths:
                try:
                    os.utime(path, None)
                except OSError:
                    pass

    @staticmethod
    def _alive(pid):
        """这个 pid 还活着吗。判不了就当活着 —— 宁可让路，不许抢活人的锁。

        POSIX 上要分清两种异常（2026-10-31 在真 Linux 上才暴露）：
        `ProcessLookupError` = 没这个进程，**真死了**（烂锁该接管）；
        `PermissionError`    = 有进程但不归我，当活着。
        以前一律当活着 → 死锁永远接不过来，这本账从此不会被压实。
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return True
        if os.name == "nt":
            try:
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
                if not h:
                    # 拿不到句柄分两种：ERROR_ACCESS_DENIED(5) = 进程在、只是不归我（提权/
                    # 别的用户会话，常态）→ **判活**，不许删活人的锁；INVALID_PARAMETER(87)
                    # = 没这个 pid → 真死了，可以接管。以前一律 return False，
                    # 正好和注释"判不了就当活着"相反，两个压实者会同时抽纸。
                    err = ctypes.windll.kernel32.GetLastError()
                    return err != 87
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            except Exception:      # noqa: BLE001
                return True
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False           # 没有这个进程：锁的主人真的没了
        except PermissionError:
            return True            # 有这个进程，只是不归我管
        except Exception:          # noqa: BLE001
            return True

    def _absorb_backup(self, bak, start=0):
        """从压实备份的 start 字节往后读完整行，重演；返回新的消费位置。

        和 `_absorb` 的区别：这条不动 `_journal_upto`（那是**当前流水**的位置），
        也不会因为读到半行就把位置推过去 —— 只推进到最后一个换行符。
        """
        try:
            size = os.path.getsize(bak)
        except OSError:
            return start
        if size <= start:
            return start
        try:
            with open(bak, "rb") as f:
                f.seek(start)
                raw = f.read(size - start)
        except OSError:
            return start
        cut = raw.rfind(b"\n") + 1
        if cut <= 0:
            return start                      # 只有半行：当没写过，下次再来
        self._replay_lines(raw[:cut].decode("utf-8", "replace").splitlines())
        return start + cut

    def _unaccounted(self, path, offset):
        """备份里从 offset 往后的行，有几行在快照里找不到对应那一节。"""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, offset))
                raw = f.read().decode("utf-8", "ignore")
        except OSError:
            return []
        miss = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                      # 半截行：本来就没判完，不算丢
            key = rec.get("key")
            if key is None:
                continue
            nid = rec.get("nid")
            for node in self.nodes.get(key, []):
                if nid and node.get("nid") == nid:
                    break
                if not nid and node.get("ts") == rec.get("ts") \
                        and node.get("reason") == rec.get("reason"):
                    break
            else:
                miss.append(line[:80])
        return miss

    def _absorb(self, jp, start=0):
        """从 start 字节往后读**完整行**，重演进账，返回新的消费位置。

        半行（写到一半断电）不猜：留到下一次读 —— 猜出来的账比缺一笔更坏。
        这是"并发不丢账"的全部秘密：只往后读，从不重写别人的行。
        """
        try:
            size = os.path.getsize(jp)
            ino = os.stat(jp).st_ino
        except OSError:
            return start
        # 偏移是"对着哪个文件"数的：压实会把流水改名、再建一个新文件，
        # 新文件跟旧文件字节数差不多时，光看"比上次的位置小"根本发现不了换代 ——
        # 于是新代开头那几行被当成"已经读过"，跳过（这就是整本连跑时
        # 0.3% 概率静默丢一节的真凶）。认文件身份，不认大小。
        known = getattr(self, "_journal_ino", None)
        if known is None:
            self._journal_ino = ino          # 头一次见：认下来，不改位置
        elif ino != known:
            start = 0                        # 换了代：偏移作废，从头读
            self._journal_ino = ino
        if size < start:
            # 流水缩水 = 换了一代（别人刚压过、把旧流水改名留底、新建了一个空的）。
            # 旧偏移是对着上一代文件的，对新文件没有意义 —— 从 0 重读，否则这一段静默跳过。
            start = 0
        if size <= start:
            return start
        try:
            with open(jp, "rb") as f:
                f.seek(start)
                raw = f.read()
        except OSError:
            return start      # 这一刻读不动（有人正拿着/目录被删）：位置不推进，下次再读，绝不假装读过
        cut = raw.rfind(b"\n") + 1
        if cut <= 0:
            return start                      # 只有半行：等它写完
        self._replay_lines(raw[:cut].decode("utf-8", "replace").splitlines())
        return start + cut

    @classmethod
    def load(cls, path, ttl=None, deliver=None, journal=None, writer=None, empty_ok=False):
        """读一本账：先读快照（有的话），再从上次消费位置接着重演流水。

        journal 默认 = path + ".jsonl"。短命进程只写流水不读快照，
        读的一方回放一次即可 —— 谁后写的谁算数。

        **读不到就报错，不假装是空账**：快照和流水一个都不存在，说明路径打错了
        （外部实测踩过：打错一个字符，拿回一本 0 节的好账，然后照样往下跑）。
        第一次读一本还没写过的账，请显式 `empty_ok=True` —— 让"读不到"和"空"长得不一样。
        """
        jp_probe = path + ".jsonl"
        if not empty_ok and not os.path.exists(path) and not os.path.exists(jp_probe) \
                and not glob.glob(jp_probe + ".*.compact"):
            raise FileNotFoundError(
                f"这本账读不到：{path} 和 {jp_probe} 都不存在。"
                "路径打错了？还是这本账还没写过 —— 第一次读新账请显式 empty_ok=True。")
        L = cls(writer=writer, ttl=ttl, deliver=deliver)
        if os.path.exists(path):
            # Windows：别人正在改这份快照时，这一下 open 会 PermissionError。
            # 那是"正在换页"，不是坏本 —— 等一下再读，别让读书的人被崩。
            last = None
            for _ in range(40):
                try:
                    with open(path, encoding="utf-8") as f:
                        d = json.load(f)
                    break
                except PermissionError as e:      # 有人正拿着：等一下再读
                    last = e
                    time.sleep(0.05)
            else:
                # 裸 raise 在 for/else 里报的是 RuntimeError("No active exception to reraise")，
                # 把真正的 PermissionError 盖掉 —— 宁可吵，也别吵一句假话。
                raise last
            L.nodes = d.get("nodes", {})
            L.open = {k: Pending.from_dict(v) for k, v in d.get("open", {}).items()}
            L.stat.update(d.get("stat", {}))
            L._journal_upto = int(d.get("_journal_upto", 0) or 0)
            L._journal_ino = d.get("_journal_ino")   # 那个位置是对着哪一代文件数的
            if L.writer is None:
                L.writer = d.get("writer")      # 快照记得这本账是谁的，不用每次重报
            L._compacted = list(d.get("compacted", []))
            L._sealed = dict(d.get("sealed", {}) or {})
            # 记住"我读的是硬盘上哪一版"：存盘前拿它比一下，变了就先合并
            L._stamp = (os.path.getmtime(path), os.path.getsize(path))
        jp = journal if journal is not None else (path + ".jsonl")
        if os.path.exists(jp):
            L._journal_upto = L._absorb(jp, L._journal_upto)
        # 压实备份里还有没吃进快照的行（压到一半崩了 / 窗口里撞进来的）：
        # 重演是幂等的，全读一遍也不会计两次 —— 宁可慢一点，不许少一行。
        for bak in sorted(glob.glob(jp + ".*.compact")):
            name = os.path.basename(bak)
            # 已登记的备份：只读登记位置之后长出来的尾巴。
            # 别人手里那只旧句柄（open 在改名之前）可以在我们登记之后再往里写一行 ——
            # 那一行必须还要读得到，否则就是账上"没发生"、世界上"发生了"。
            if name in L._compacted and name in L._sealed:
                start = int(L._sealed.get(name) or 0)
                if start < os.path.getsize(bak):
                    L._sealed[name] = L._absorb_backup(bak, start)
                continue
            if name in L._compacted:
                continue                      # 老快照没记位置：按老规矩整份不读
            L._sealed[name] = L._absorb_backup(bak, 0)
            L._compacted.append(name)
        return L

    def replay(self, journal):
        """把整本流水从头重演一遍（调试用）。日常走 load/_absorb，接着读不重复。"""
        if not os.path.exists(journal):
            return 0
        with open(journal, encoding="utf-8") as f:
            return self._replay_lines(f.read().splitlines())

    def _replay_lines(self, lines):
        """一行一行重演 —— 这不是"加载"，是重演。半行当没写过，不猜。"""
        n = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                # 不是 JSON 也算一行坏行：写到一半被断电劈开的那一行就是这样。
                # 以前只静默跳过、不记账 —— 于是"账看起来全读了"，其实少了几行。
                self.stat["bad_lines"] = self.stat.get("bad_lines", 0) + 1
                continue
            t = rec.get("t")
            key = rec.get("key")
            try:
                self._replay_one(t, key, rec)
            except Exception:
                # 一行坏行不许锁死整本账：流水是正本，正本里混进一条手写的、
                # 旧版本写的、写到一半的行，都不该让 load() 抛错 —— 那等于
                # 让一行坏数据把过去几千条判决一起关在门外。记一笔，跳过，继续。
                self.stat["bad_lines"] = self.stat.get("bad_lines", 0) + 1
                continue
            n += 1
        self.stat["replayed"] = self.stat.get("replayed", 0) + n
        return n

    def _replay_one(self, t, key, rec):
        """一类一行。坏行由调用方记 bad_lines，这里只管按规矩重演。"""
        if t == "node" and rec.get("node"):
            self._replay_node(key, rec["node"])
        elif t == "pending" and rec.get("p"):
            p = Pending.from_dict(rec["p"])
            prev = self.open.get(key)
            self.open[key] = p if (prev is None or p.tries >= prev.tries) else prev
        elif t == "hit":
            # 一借一行：多进程各写各的，回放时累加，谁也不盖谁。
            # 每行带 hid：换代重读（压实改名后从 0 重演、备份再扫一遍）时按 hid 认账，
            # 同一笔借不会因为"读了两遍"变成两次。老流水没有 hid，照旧按到达算。
            hid = rec.get("hid")
            if hid and hid in self._hit_seen:
                return
            if hid:
                self._hit_seen.add(hid)
            ch = self.nodes.get(key) or []
            r = rec.get("round") or 1
            if 1 <= r <= len(ch) and not self.is_tombstone(ch[r - 1]):
                nd = ch[r - 1]
                nd["hits"] = int(nd.get("hits") or 0) + 1
                ts = rec.get("ts")
                if ts and (nd.get("last_hit") is None or ts > nd["last_hit"]):
                    nd["last_hit"] = ts
                nd.setdefault("borrows", []).append({
                    "from_round": r, "checked": bool(rec.get("checked")),
                    "ts": ts, "by": rec.get("by")})
        elif t == "resolve":
            # 冷回放时账上本来就没有那笔悬位（悬位只活在开它的那个进程里），
            # 但写的一方明确说了 had=True —— 那就是"确实收掉了一笔"，
            # 不认这一条，"收口了几笔"在重演出来的账上永远是 0。
            closed = self.open.pop(key, None) is not None or rec.get("had") is True
            if rec.get("forced"):
                self.stat["forced"] = self.stat.get("forced", 0) + 1
            if closed:
                ident = (key, rec.get("ts"))
                if ident not in self._resolved_seen:
                    self._resolved_seen.add(ident)
                    self.stat["resolved"] = self.stat.get("resolved", 0) + 1
            if rec.get("node"):
                self._replay_node(key, rec["node"])

    def _replay_node(self, key, node):
        """把一节账放回它该在的位置。圈号缺了就按顺序补 —— 位置由重演决定。"""
        node = dict(node)
        # 流水是正本，正本也要过尺：手写的、旧版本写坏的、写了一半的判决，
        # 不许顺着重演直接进链。以前这把尺只装在笔上（record/append/check），
        # 门框上没有 —— 手写一行 verdict='对' 冷读照收，antipode 当场崩、six_port 照报有数。
        v = node.get("verdict")
        if not (v is None or (type(v) is int and v in (0, 1))):
            raise ValueError(f"流水里这一节的判决不是 1/0/None：{v!r}")
        if node.get("tombstone") is not None and v is not None:
            raise ValueError("墓碑不许带判决")
        chain = self.nodes.get(key) or []
        r = node.get("round")
        # 重演必须幂等，判据是**这一节的内容**，不是 (ts, verdict) 两样。
        # 旧判据两头都会错（2026-10-31 外部评审A评审指出，实测复现）：
        #   · 同 ts 同 verdict 但确属两圈 → 被当重复丢掉；
        #   · 同圈不同 ts → 再 append 一节，链上出现 [1,2,3,2]，flips() 报出不存在的翻面。
        if r and 1 <= r <= len(chain) and self._same_node(chain[r - 1], node):
            return
        if not r:
            # 流水里圈号是空的：写的那一方手里只有今天这一节，它不知道自己是第几圈
            # （圈号属于重演出来的位置）。所以这里按**内容**认人：账上已经有一节
            # 一模一样的，就是同一节，不许再记一遍。
            # 认人先认身份（nid），没有身份才退回比内容 —— 只比内容的话，
            # 这节在内存里被回填过分、补过指纹、或被墓碑化，内容就变了，
            # 于是自己那行重演时被当成新的一节：判决史翻倍，删掉的节还会以新圈号复活。
            for old in chain:
                if self._same_node({k: v for k, v in old.items() if k != "round"},
                                   {k: v for k, v in node.items() if k != "round"}):
                    return
        # 圈号被别的内容占了 / 跳号 / 没圈号：位置由到达顺序决定，不抢别人的圈号
        if not r or r != len(chain) + 1:
            # 只有"正好接在队尾"才保留原圈号；占着别人的圈号或跳号，一律顺延
            node["round"] = len(chain) + 1
        self.nodes.setdefault(key, []).append(node)
        return node

    # 会自己变的字段，不算内容（探针借一次这两样就动，它们是统计不是判决）
    _VOLATILE = ("hits", "last_hit", "borrows")

    @classmethod
    def _same_node(cls, a, b):
        """两节是不是同一节：先认身份，没有身份才退回比内容。

        只比内容会两头出错：这节被改过（回填分/补指纹/墓碑化）就说"不是同一节"，
        于是同一节被记两遍；两节碰巧内容一样又说"是同一节"，于是丢一节。
        老账没有 `nid`，仍走比内容那条路 —— 能读，但别指望它精确。
        """
        na, nb = a.get("nid"), b.get("nid")
        if na and nb:
            return na == nb
        ka = {k: v for k, v in a.items() if k not in cls._VOLATILE}
        kb = {k: v for k, v in b.items() if k not in cls._VOLATILE}
        return ka == kb


class LedgerGanglion:
    """骨 + 账：骨只当反射，记账权在账本。

    check() 出去的三个值：
        1 / 0     塌缩了，落账
        Pending   还在走，留悬位，不落账
    """

    def __init__(self, ledger=None, why_if_unknown=EVIDENCE, journal=None):
        """journal : 借票落哪本流水。**不给就构造不出来** —— 骨+账的入口
        一旦会借，就必须落账；不然多进程互相盖 hits，prune() 就借得越多忘得越快。
        确实不要落账就明写 journal=False（只许单进程）。
        """
        if journal is None:
            raise ValueError(
                "LedgerGanglion 会借账，借票必须有地方落：给 journal=<流水路径>，"
                "或明写 journal=False（只许单进程）")
        try:
            from ganglion import Ganglion, _make_key
        except ImportError as e:
            raise ImportError(
                "LedgerGanglion 需要外挂的反射标准件 ganglion.py（独立仓库，本发布包不携带）："
                "请把 ganglion.py 与本文件装在同一目录（或加入 PYTHONPATH）后再用；"
                "核心账本不依赖它，只记只借可直接用 Ledger。"
            ) from e
        self._make_key = _make_key
        self.journal = journal
        self.ledger = ledger if ledger is not None else Ledger()
        self._g = Ganglion(mem=None)          # 本体不记忆，纯反射
        self.why_if_unknown = why_if_unknown
        self.stat = self.ledger.stat

    def check(self, content, processor=None, ctx_fp="", checked=False):
        key = self._make_key(content)

        node = self.ledger.probe(key, ctx_fp, checked=checked, journal=self.journal)
        if node is not None:
            tag = "借时已核" if checked else "借时未核"
            return node["verdict"], f"[借账 第{node['round']}圈·{tag}] {node['reason']}"

        # 三值协议里 processor 返一个 Pending 是**正常答案**（还在走）。
        # 骨本体按元组解包，拿到 Pending 会抛 "cannot unpack non-iterable"，
        # 再把这条英文异常当成"检测异常"记进 need —— 照协议写反而崩，是我们没接住。
        # 这里把它翻译成骨节认得的"判不了"（None + 缺什么），why 仍用悬位自己带的质地。
        if processor is not None:
            inner = processor

            def processor(c):
                r = inner(c)
                if isinstance(r, Pending):
                    return None, r.need
                if isinstance(r, tuple) and r and isinstance(r[0], Pending):
                    return None, (r[1] or r[0].need)
                return r

        verdict, reason = self._g.check(content, processor)

        # 三值协议里"还在走"就是返一个 Pending —— 这是正常答案，不是异常。
        # 以前它顺着往下走：去 record() 被"空不入账"顶回来抛 ValueError，
        # 或者被当成"检测异常"，把悬位质地写成 evidence、need 写成 TypeError 的英文原文。
        # 照协议返回反而崩，这不是用错，是我们没接住。
        if isinstance(verdict, Pending):
            p = self.ledger.open_pending(
                key, why=getattr(verdict, "why", self.why_if_unknown) or self.why_if_unknown,
                need=getattr(verdict, "need", "") or (reason or ""), ctx_fp=ctx_fp)
            if self.journal is not False:
                self.ledger._write_line(self.journal,
                                        {"t": "pending", "key": key, "p": p.to_dict()})
            self.stat["unknown"] += 1
            return p, (reason or getattr(verdict, "need", ""))

        if verdict is None:
            # 空有三种质地，别混：账里有旧节却配不上（或过窗），是"该重算"，不是"判不了"
            miss = self.ledger.why_not(key)
            why = MISMATCH if miss in (MISMATCH, EXPIRED) else self.why_if_unknown
            p = self.ledger.open_pending(key, why=why, need=reason, ctx_fp=ctx_fp)
            # 悬位也要落盘。挂在这里的短命进程（一分钟一次）下次进来是**另一个进程**，
            # 悬位只在内存里的话，"这笔我还没收到料"当场失忆，tries 又从头数 ——
            # 该收口的永远收不了，而且账面上看不出来。
            if self.journal is not False:
                self.ledger._write_line(self.journal,
                                        {"t": "pending", "key": key, "p": p.to_dict()})
            self.stat["unknown"] += 1
            return p, reason

        node = self.ledger.record(key, verdict, reason=reason, ctx_fp=ctx_fp)
        # 判出来的这一节**必须**落流水，不能只活在内存里。
        # 以前只有借票落盘、判决不落：短命进程（一分钟跑一次的探针）用这个口，
        # 重启之后判决全丢、processor 被重新调用，而账面上 journal 配得好好的。
        # 圈号留给重演补（写的人不知道自己是第几圈）；重演按内容认人，不会记两遍。
        if self.journal is not False:
            self.ledger._write_line(self.journal,
                                    {"t": "node", "key": key,
                                     "node": dict(node, round=None)})
        self.stat["computed"] += 1
        return verdict, reason
