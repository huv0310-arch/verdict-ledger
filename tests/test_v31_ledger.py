"""v3.1 两件迭代项的验收：时间线查询 recent(n) 与半行残尾自愈。

全部在本次运行新建的临时目录里开账 —— 已有的账一个字节不碰。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "V2", "v2_gate"))
from ledger import Ledger  # noqa: E402


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


class RecentCase(unittest.TestCase):
    """recent(n)：整本账按节自带的钟穿成一条时间线，只读。"""

    def setUp(self):
        self.L = Ledger(writer="unit-test")

    def test_recent_sorts_newest_first_across_addresses(self):
        L = self.L
        L.record("addr-a", 1, ts=100.0)
        L.record("addr-b", 1, ts=300.0)
        L.record("addr-a", 0, ts=200.0)
        out = L.recent(2)
        self.assertEqual([n["key"] for n in out], ["addr-b", "addr-a"])
        self.assertEqual(out[1]["round"], 2)          # 同地址取的是最新那一圈
        self.assertEqual(out[1]["verdict"], 0)
        self.assertEqual(L.recent(10)[2]["verdict"], 1)   # 最老那节是 addr-a 第 1 圈

    def test_recent_boundaries_and_argument_check(self):
        L = self.L
        L.record("addr-a", 1, ts=1.0)
        self.assertEqual(L.recent(0), [])
        self.assertEqual(L.recent(-3), [])
        self.assertEqual(len(L.recent(10)), 1, "不够 n 节就全给")
        with self.assertRaises(ValueError):
            L.recent("3")
        with self.assertRaises(ValueError):
            L.recent(3.0)

    def test_recent_skips_tombstones_but_keeps_faded_nodes(self):
        L = self.L
        L.record("addr-a", 1, ts=10.0, ttl=5)
        L.record("addr-a", 1, ts=20.0, ttl=500)
        L.record("addr-a", 1, ts=30.0, ttl=500)
        arc = os.path.join(tempfile.mkdtemp(prefix="v31_arc_"), "expired.jsonl")
        self.assertEqual(L.sweep_ttl(now=100.0, archive=arc), 1)   # 出窗那节变墓碑
        self.assertTrue(os.path.exists(arc), "先归档后动刀")
        L.prune(key="addr-a", keep_last=1)                        # 前圈褪成梗概
        out = L.recent(10)
        self.assertEqual(len(out), 2, "墓碑不在时间线上，褪色的那节还在账上")
        self.assertEqual([n["ts"] for n in out], [30.0, 20.0])
        self.assertTrue(out[1].get("demoted"), "褪色节（梗概）要算数")

    def test_recent_is_readonly(self):
        L = self.L
        L.record("addr-a", 1, ts=1.0)
        L.record("addr-b", 0, ts=2.0)
        before = repr(L.to_dict())
        L.recent(5)
        L.recent(0)
        self.assertEqual(before, repr(L.to_dict()), "recent 不改链、不动统计、不落盘")


class TornTailCase(unittest.TestCase):
    """半行残尾：上一个写者死在半路留下的半行，不许粘住下一次追加。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v31_tail_")
        self.jp = os.path.join(self.dir, "ledger.jsonl")

    def fill(self, n=2):
        L = Ledger(writer="unit-test")
        for i in range(n):
            L.append(self.jp, "addr-%d" % i, 1, "reason")
        return L

    def tear(self):
        """模拟写到一半断电：半行、没有换行。"""
        with open(self.jp, "ab") as f:
            f.write(b'{"t": "node", "key": "addr-9", "node": {"ver')

    def test_next_append_heals_the_tail_and_keeps_every_line(self):
        self.fill(2)
        self.tear()
        Ledger(writer="unit-test").append(self.jp, "addr-9", 1, "reason")
        raw = _bytes(self.jp)
        self.assertTrue(raw.endswith(b"\n"), "追加之后尾巴必须是完整的行")
        L2 = Ledger(writer="unit-test")
        self.assertEqual(L2.replay(self.jp), 3)
        self.assertEqual(L2.stat.get("bad_lines", 0), 0, "一条坏行都不许有")
        self.assertEqual(sorted(L2.nodes), ["addr-0", "addr-1", "addr-9"],
                         "崩在半路的那一行不许把新写的那节带走")

    def test_without_the_heal_the_new_line_is_swallowed(self):
        """对照（修复前的行为，把自愈关掉就复现）：坏一行、丢一行。"""
        self.fill(2)
        self.tear()
        original = Ledger.__dict__["_heal_torn_tail"]
        Ledger._heal_torn_tail = staticmethod(lambda path: False)
        try:
            Ledger(writer="unit-test").append(self.jp, "addr-9", 1, "reason")
        finally:
            Ledger._heal_torn_tail = original
        L2 = Ledger(writer="unit-test")
        L2.replay(self.jp)
        self.assertNotIn("addr-9", L2.nodes, "修复前：新写的那节整节没进账")
        self.assertEqual(L2.stat.get("bad_lines", 0), 1)

    def test_only_the_torn_tail_is_cut(self):
        self.fill(3)
        before = _bytes(self.jp).splitlines(keepends=True)
        self.tear()
        Ledger(writer="unit-test").append(self.jp, "addr-9", 1, "reason")
        after = _bytes(self.jp).splitlines(keepends=True)
        self.assertEqual(after[:3], before, "前面几行一个字节都不许动")

    def test_normal_tail_is_untouched(self):
        self.fill(2)
        before = _bytes(self.jp)
        self.assertFalse(Ledger._heal_torn_tail(self.jp), "正常尾巴不用治")
        self.assertEqual(before, _bytes(self.jp))

    def test_missing_journal_is_not_an_error(self):
        p = os.path.join(self.dir, "absent.jsonl")
        self.assertFalse(Ledger._heal_torn_tail(p))
        self.assertFalse(os.path.exists(p), "没有尾巴可治，也不许凭空建文件")


if __name__ == "__main__":
    unittest.main()
