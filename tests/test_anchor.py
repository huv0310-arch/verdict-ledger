"""链头锚定（v3.1）验收：链头摘要 / 追加锚 / 截断核验 / 命令行。

账目录由 V2_STATE_DIR 指到本次运行新建的临时目录 —— 只在临时目录里开账，
已有的账一个字节不碰。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE = os.path.join(ROOT, "V2", "v2_gate.py")

_STATE = tempfile.mkdtemp(prefix="v31_anchor_")
os.environ["V2_STATE_DIR"] = _STATE
os.environ.pop("V2_ANCHORS", None)
sys.path.insert(0, os.path.join(ROOT, "V2"))
import v2_gate as G  # noqa: E402


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _run(argv):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable] + argv, cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


class AnchorCase(unittest.TestCase):
    def setUp(self):
        for fn in ("journal.jsonl", "journal.jsonl.chain", "ledger.json", "anchors.jsonl"):
            p = os.path.join(_STATE, fn)
            if os.path.exists(p):
                os.remove(p)
        self.journal = os.path.join(_STATE, "journal.jsonl")
        self.chain = self.journal + ".chain"
        self.anchors = os.path.join(_STATE, "anchors.jsonl")

    def fill(self, n=3):
        """开 n 笔互不相同的判决（归一化会去数字，所以用不重样的字来分 key；
        同值重复会被护栏拦下，不长行）。"""
        marks = "甲乙丙丁戊己庚辛"
        for i in range(n):
            self.assertTrue(G.record("demo", "示例问法" + marks[i], 1, "kind"))

    def test_chain_head_is_readonly_and_carries_count_digest_and_time(self):
        self.fill(3)
        before = (os.path.getsize(self.journal), os.path.getsize(self.chain))
        h = G.chain_head()
        self.assertTrue(h["ok"], h)
        self.assertEqual(h["lines"], 3)
        self.assertEqual(h["chained"], 3)
        self.assertTrue(h["head"].startswith("sha256:"), h)
        self.assertEqual(len(h["head"]), len("sha256:") + 64)
        self.assertTrue(h["ts"] > 0)
        self.assertEqual(before, (os.path.getsize(self.journal), os.path.getsize(self.chain)),
                         "chain_head 只读：账与影子链一个字节都不许动")

    def test_anchor_appends_then_verify_passes_and_keeps_passing_as_the_ledger_grows(self):
        self.fill(3)
        a = G.anchor()
        self.assertTrue(a["ok"], a)
        self.assertEqual(a["count"], 3)
        rows = [json.loads(x) for x in _read(self.anchors).splitlines() if x.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"ts", "count", "head", "algo"})
        v = G.verify_against()
        self.assertTrue(v["ok"], v)
        self.fill(4)                       # 账在锚之后继续长
        v2 = G.verify_against()
        self.assertTrue(v2["ok"], v2)
        self.assertIn("继续长了 1 行", v2["why"])

    def test_tail_truncation_is_caught_even_when_the_chain_stays_self_consistent(self):
        """把 journal 和影子链一起截掉两行：剩下的链条依然自洽 —— 单看链抓不到，锚抓得到。"""
        self.fill(4)
        G.anchor()
        jrows = [l for l in _read(self.journal).splitlines() if l.strip()]
        crows = [l for l in _read(self.chain).splitlines() if l.strip()]
        _write(self.journal, "\n".join(jrows[:2]) + "\n")
        _write(self.chain, "\n".join(crows[:2]) + "\n")
        self.assertTrue(G.verify_chain()["ok"], "单看影子链：截断后依然自洽（这正是缺口）")
        v = G.verify_against()
        self.assertFalse(v["ok"], v)
        self.assertTrue(v.get("truncated"))
        self.assertIn("截断", v["why"])

    def test_anchor_file_missing_or_empty_is_never_reported_green(self):
        self.fill(2)
        v = G.verify_against()
        self.assertFalse(v["ok"])
        self.assertIn("锚文件不存在", v["why"])
        _write(self.anchors, "")
        v = G.verify_against()
        self.assertFalse(v["ok"])
        self.assertIn("空", v["why"])

    def test_row_edited_before_the_anchor_is_caught(self):
        self.fill(3)
        G.anchor()
        rows = [l for l in _read(self.journal).splitlines() if l.strip()]
        obj = json.loads(rows[1])
        obj["node"]["verdict"] = 0
        rows[1] = json.dumps(obj, ensure_ascii=False)
        _write(self.journal, "\n".join(rows) + "\n")
        v = G.verify_against()
        self.assertFalse(v["ok"], v)
        self.assertFalse(v.get("truncated"))
        self.assertIn("对不上", v["why"])

    def test_anchor_refuses_to_invent_a_head_for_an_empty_ledger(self):
        a = G.anchor()
        self.assertFalse(a["ok"], a)
        self.assertIn("0 行", a["why"])
        self.assertFalse(os.path.exists(self.anchors), "没得锚就不许留下锚文件")

    def test_anchor_writes_only_the_anchor_file_and_appends_each_time(self):
        self.fill(2)
        before = (_bytes(self.journal), _bytes(self.chain))
        self.assertTrue(G.anchor()["ok"])
        self.assertTrue(G.anchor()["ok"])
        self.assertEqual(before, (_bytes(self.journal), _bytes(self.chain)))
        rows = [l for l in _read(self.anchors).splitlines() if l.strip()]
        self.assertEqual(len(rows), 2, "锚文件是 append-only：一笔一行，不覆盖")

    def test_cli_chain_head_and_plain_verify(self):
        self.fill(2)
        p = _run([GATE, "chain-head"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out["lines"], 2)
        self.assertTrue(out["head"].startswith("sha256:"))
        p2 = _run([GATE, "verify"])
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertTrue(json.loads(p2.stdout.strip().splitlines()[-1])["ok"])

    def test_cli_verify_against_exits_nonzero_on_truncation(self):
        self.fill(3)
        G.anchor()
        jrows = [l for l in _read(self.journal).splitlines() if l.strip()]
        crows = [l for l in _read(self.chain).splitlines() if l.strip()]
        _write(self.journal, "\n".join(jrows[:1]) + "\n")
        _write(self.chain, "\n".join(crows[:1]) + "\n")
        p = _run([GATE, "verify", "--against", self.anchors])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertFalse(out["ok"])
        self.assertTrue(out["truncated"])


if __name__ == "__main__":
    unittest.main()
