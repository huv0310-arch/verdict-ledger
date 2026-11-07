"""v3.2 验收：签名链头 / 一致性检查 / equivocation 报警 / 一致性证明挑战单。

账目录只在本次运行新建的临时目录里开 —— 已有的账一个字节不碰；
签名密钥只在本进程内生成一把测试钥，仓里不放任何真密钥。
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE = os.path.join(ROOT, "V2", "v2_gate.py")

_STATE = tempfile.mkdtemp(prefix="v32_chal_")
# 已经有测试模块把账目录指到临时目录时沿用它（setdefault）：不许把别人 subprocess 的账指跑偏
os.environ.setdefault("V2_STATE_DIR", _STATE)
os.environ.pop("V2_ANCHORS", None)
os.environ.pop("V2_CHALLENGES", None)
os.environ.pop("V2_PROOF_DEADLINE", None)
os.environ.setdefault("V2_ANCHOR_KEY", secrets.token_hex(16))
sys.path.insert(0, os.path.join(ROOT, "V2"))
import v2_gate as G  # noqa: E402


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _rows(p):
    return [json.loads(x) for x in _read(p).splitlines() if x.strip()]


def _append(p, obj):
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _run(argv):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable] + argv, cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


def _last_json(out):
    return json.loads(out.strip().splitlines()[-1])


class Base(unittest.TestCase):
    def setUp(self):
        for fn in ("journal.jsonl", "journal.jsonl.chain", "ledger.json",
                   "anchors.jsonl", "challenges.jsonl"):
            p = os.path.join(G.STATE_DIR, fn)
            if os.path.exists(p):
                os.remove(p)
        self.state = G.STATE_DIR
        self.journal = os.path.join(self.state, "journal.jsonl")
        self.chain = self.journal + ".chain"
        self.anchors = os.path.join(self.state, "anchors.jsonl")
        self.chal = os.path.join(self.state, "challenges.jsonl")
        os.environ.pop("V2_PROOF_DEADLINE", None)

    def fill(self, n=3):
        marks = "甲乙丙丁戊己庚辛壬癸"
        for i in range(n):
            self.assertTrue(G.record("demo", "示例问法" + marks[i], 1, "kind"))

    def keyset(self):
        key, kid, all_keys, err = G._load_keys()
        self.assertEqual(err, "", err)
        self.assertIsNotNone(key)
        return key, kid, all_keys


class SignedHeadCase(Base):
    """签名链头：(entry_count, root_hash, issued_at, key_id) + 签名，追加进锚文件。"""

    def test_anchor_row_is_a_signed_head_and_verification_stays_green(self):
        self.fill(3)
        a = G.anchor(path=self.anchors)
        self.assertTrue(a["ok"], a)
        self.assertEqual(set(a) & {"entry_count", "root_hash", "issued_at", "key_id", "sig"},
                         {"entry_count", "root_hash", "issued_at", "key_id", "sig"})
        row = _rows(self.anchors)[0]
        self.assertEqual(set(row), {"v", "algo", "entry_count", "root_hash",
                                    "issued_at", "key_id", "sig"})
        self.assertTrue(row["key_id"].startswith("hmac-sha256:"), row["key_id"])
        self.assertEqual(row["entry_count"], 3)
        self.assertTrue(row["root_hash"].startswith("sha256:"))
        self.assertTrue(row["issued_at"] > 0)
        key, kid, all_keys = self.keyset()
        self.assertEqual(row["key_id"], kid)
        self.assertTrue(G._sig_ok(G._anchor_payload(G._anchor_norm(row)),
                                  row["sig"], all_keys), "签名必须核得过")
        self.assertTrue(G.check_anchors(self.anchors)["ok"])
        self.assertTrue(G.verify_against(self.anchors)["ok"])

    def test_tampering_with_a_signed_anchor_row_breaks_the_signature(self):
        self.fill(2)
        G.anchor(path=self.anchors)
        row = _rows(self.anchors)[0]
        row["root_hash"] = "sha256:" + "0" * 64          # 改根不改签名
        _write(self.anchors, json.dumps(row, ensure_ascii=False) + "\n")
        chk = G.check_anchors(self.anchors, recompute=False)
        self.assertFalse(chk["ok"], chk)
        self.assertTrue(any("签名核不过" in p for p in chk["problems"]), chk["problems"])
        v = G.verify_against(self.anchors)
        self.assertFalse(v["ok"], v)
        self.assertIn("核不过", v["why"])

    def test_missing_key_is_a_clear_error_and_writes_no_anchor(self):
        self.fill(2)
        saved = (os.environ.pop("V2_ANCHOR_KEY", None),
                 os.environ.pop("V2_ANCHOR_KEY_FILE", None))
        try:
            a = G.anchor(path=self.anchors)
        finally:
            if saved[0] is not None:
                os.environ["V2_ANCHOR_KEY"] = saved[0]
            if saved[1] is not None:
                os.environ["V2_ANCHOR_KEY_FILE"] = saved[1]
        self.assertFalse(a["ok"], a)
        self.assertIn("密钥", a["why"])
        self.assertIn("V2_ANCHOR_KEY", a["why"])
        self.assertFalse(os.path.exists(self.anchors), "没签名就不许落锚")

    def test_v31_format_anchor_rows_still_read_as_unsigned(self):
        """老格式锚行（v3.1：ts/count/head/algo）必须继续可读，按无签名处理。"""
        self.fill(3)
        n, head = G._head_of(G._journal_rows(), 3)
        _write(self.anchors, json.dumps({"ts": time.time(), "count": n,
                                         "head": "sha256:" + head, "algo": "sha256-chain"},
                                        ensure_ascii=False) + "\n")
        rows, err = G._read_anchors(self.anchors)
        self.assertIsNone(err, err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_count"], 3)
        self.assertIsNone(rows[0]["sig"], "老格式行没有签名字段，按无签名处理")
        chk = G.check_anchors(self.anchors)
        self.assertTrue(chk["ok"], chk)
        self.assertTrue(any("老格式" in x for x in chk["notes"]), chk["notes"])
        self.assertTrue(G.verify_against(self.anchors)["ok"],
                        "老格式锚行照样能拿来对账")


class ConsistencyCase(Base):
    """一致性检查：新锚必须是旧锚的 append-only 延伸，倒退 / 跳跃 / 对不上一律报红。"""

    def _fake(self, count, root):
        return {"v": 2, "algo": "sha256-chain", "entry_count": count,
                "root_hash": "sha256:" + root, "issued_at": time.time(),
                "key_id": "hmac-sha256:test", "sig": None}

    def test_count_regression_is_red(self):
        rows = [self._fake(5, "a" * 64), self._fake(3, "b" * 64)]
        _write(self.anchors, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        chk = G.check_anchors(self.anchors, recompute=False)
        self.assertFalse(chk["ok"], chk)
        self.assertTrue(any("倒退" in p for p in chk["problems"]), chk["problems"])
        v = G.verify_against(self.anchors)
        self.assertFalse(v["ok"], v)
        self.assertIn("倒退", v["why"])

    def test_count_jump_with_a_frozen_root_is_red(self):
        rows = [self._fake(3, "c" * 64), self._fake(5, "c" * 64)]
        _write(self.anchors, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        chk = G.check_anchors(self.anchors, recompute=False)
        self.assertFalse(chk["ok"], chk)
        self.assertTrue(any("跳跃" in p for p in chk["problems"]), chk["problems"])

    def test_recompute_mismatch_is_red(self):
        self.fill(3)
        G.anchor(path=self.anchors)
        rows = [json.loads(x) for x in _read(self.journal).splitlines() if x.strip()]
        rows[1]["node"]["verdict"] = 0                      # 事后改账
        _write(self.journal, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        chk = G.check_anchors(self.anchors)
        self.assertFalse(chk["ok"], chk)
        self.assertTrue(any("重算对不上" in p for p in chk["problems"]), chk["problems"])


class EquivocationCase(Base):
    """同 entry_count、不同 root_hash 的两个签名头 = 自包含的作弊收据，必须明确报警。"""

    def test_same_count_different_root_is_reported_with_both_heads(self):
        self.fill(2)
        G.anchor(path=self.anchors)
        key, kid, all_keys = self.keyset()
        payload = {"entry_count": 2, "root_hash": "sha256:" + "f" * 64,
                   "issued_at": time.time(), "key_id": kid}
        fork = {"v": 2, "algo": "sha256-chain"}
        fork.update(payload)
        fork["sig"] = G._hmac_sign(key, G._canon(payload))
        _append(self.anchors, fork)
        chk = G.check_anchors(self.anchors, recompute=False)
        self.assertFalse(chk["ok"], "同条数异根必须报红，不许静默")
        self.assertEqual(len(chk["equivocations"]), 1, chk)
        pair = chk["equivocations"][0]
        self.assertEqual(pair["entry_count"], 2)
        self.assertEqual(pair["a"], _rows(self.anchors)[0], "第一个头要原样列出")
        self.assertEqual(pair["b"], _rows(self.anchors)[1], "第二个头要原样列出")
        self.assertTrue(any("equivocation" in p for p in chk["problems"]), chk["problems"])
        v = G.verify_against(self.anchors)
        self.assertFalse(v["ok"], v)
        self.assertTrue(v["equivocations"], "对账时也要带着这对头，不能只报一句失败")
        p = _run([GATE, "check-anchors", "--against", self.anchors])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("equivocation", p.stdout)

    def test_reanchoring_the_same_head_is_not_a_fork(self):
        self.fill(2)
        G.anchor(path=self.anchors)
        G.anchor(path=self.anchors)
        chk = G.check_anchors(self.anchors)
        self.assertTrue(chk["ok"], chk)
        self.assertEqual(chk["equivocations"], [])


class ChallengeCase(Base):
    """一致性证明挑战单：签名请求单 + 送达回执 + 书面 deadline。"""

    def _issue(self, issued_at, deadline=300.0, rid=None, from_count=1, to_count=3):
        """手工签一张旧请求单（模拟早就发出去、一直没回执 / 没答复的单）。"""
        key, kid, _ = self.keyset()
        rid = rid or ("ch-%d-test" % int(issued_at))
        payload = {"request_id": rid, "from_count": from_count, "to_count": to_count,
                   "issued_at": issued_at, "deadline": deadline, "key_id": kid}
        body = G._canon(payload)
        _append(self.chal, {"type": "request", "request_id": rid,
                            "from_count": from_count, "to_count": to_count,
                            "issued_at": issued_at, "deadline": deadline,
                            "body": body, "signature": G._hmac_sign(key, body),
                            "key_id": kid})
        return rid

    def _deliver(self, rid, at):
        key, kid, _ = self.keyset()
        body = G._canon({"request_id": rid, "delivered_at": at})
        _append(self.chal, {"type": "delivery", "request_id": rid, "delivered_at": at,
                            "body": body, "signature": G._hmac_sign(key, body),
                            "key_id": kid})

    def test_issue_writes_a_signed_request_with_the_default_deadline(self):
        c = G.challenge(1, 3, path=self.chal)
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["deadline"], 300.0, "默认 deadline 必须是 300 秒")
        self.assertTrue(c["request_id"].startswith("ch-"), c["request_id"])
        row = _rows(self.chal)[0]
        self.assertEqual(row["type"], "request")
        self.assertIsNone(row.get("delivered_at"), "回执是另一笔事件，不许跟请求混在一起")
        _, _, all_keys = self.keyset()
        payload = {"request_id": row["request_id"], "from_count": row["from_count"],
                   "to_count": row["to_count"], "issued_at": row["issued_at"],
                   "deadline": row["deadline"], "key_id": row["key_id"]}
        self.assertEqual(row["body"], G._canon(payload))
        self.assertTrue(G._sig_ok(payload, row["signature"], all_keys))

    def test_deadline_is_overridable_and_a_bad_value_is_reported(self):
        os.environ["V2_PROOF_DEADLINE"] = "45"
        try:
            self.assertEqual(G.challenge(1, 2, path=self.chal)["deadline"], 45.0)
            os.environ["V2_PROOF_DEADLINE"] = "不是数"
            bad = G.challenge(1, 2, path=self.chal)
            self.assertFalse(bad["ok"], bad)
            self.assertIn("V2_PROOF_DEADLINE", bad["why"])
        finally:
            os.environ.pop("V2_PROOF_DEADLINE", None)

    def test_missing_delivery_receipt_is_never_judged_a_failure(self):
        """缺回执 → undelivered：过期也不许判「拒绝证明」（退出码 0）。"""
        rid = self._issue(time.time() - 100000)
        st = G.challenge_status(path=self.chal)
        self.assertTrue(st["ok"], st)
        self.assertFalse(st["judged_timeout"], st)
        self.assertEqual(st["challenges"][0]["status"], "undelivered")
        self.assertIsNone(st["challenges"][0]["delivered_at"])
        self.assertIn("不判失败", st["challenges"][0]["why"])
        p = _run([GATE, "challenge-status", "--path", self.chal])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(_last_json(p.stdout)["challenges"][0]["status"], "undelivered")

    def test_timeout_is_judged_only_after_the_receipt_is_on_file(self):
        issued = time.time() - 100000
        rid = self._issue(issued)
        before = G.challenge_status(path=self.chal)
        self.assertFalse(before["judged_timeout"], "回执都没留，不许判失败")
        self._deliver(rid, issued + 1)
        st = G.challenge_status(path=self.chal)
        self.assertTrue(st["judged_timeout"], st)
        self.assertFalse(st["ok"], st)
        row = st["challenges"][0]
        self.assertEqual(row["status"], "timeout")
        self.assertIsNotNone(row["delivered_at"], "超时判定必须读回执字段")
        self.assertIn("拒绝证明", row["why"])
        p = _run([GATE, "challenge-status", "--path", self.chal])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(_last_json(p.stdout)["challenges"][0]["status"], "timeout")

    def test_delivered_within_the_deadline_is_pending_not_failed(self):
        issued = time.time() - 10
        rid = self._issue(issued)
        self._deliver(rid, issued + 1)
        st = G.challenge_status(path=self.chal)
        self.assertTrue(st["ok"], st)
        self.assertFalse(st["judged_timeout"], st)
        self.assertEqual(st["challenges"][0]["status"], "pending")
        p = _run([GATE, "challenge-status", "--path", self.chal])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_delivered_and_answered_challenge_verifies_green(self):
        self.fill(4)
        c = G.challenge(1, 3, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        r = G.challenge_respond(c["request_id"], path=self.chal)
        self.assertTrue(r["ok"], r)
        self.assertTrue(G.check_proof(r["response"])[0], r["response"])
        st = G.challenge_status(c["request_id"], path=self.chal)
        self.assertTrue(st["ok"], st)
        row = st["challenges"][0]
        self.assertEqual(row["status"], "answered")
        self.assertTrue(row["proof_ok"], row)
        p = _run([GATE, "challenge-status", c["request_id"], "--path", self.chal])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(_last_json(p.stdout)["challenges"][0]["status"], "answered")

    def test_tampered_request_is_flagged_instead_of_trusted(self):
        rid = self._issue(time.time() - 5, from_count=1, to_count=3)
        rows = _rows(self.chal)
        rows[0]["from_count"] = 9                    # 改单据正文，签名不动
        _write(self.chal, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        st = G.challenge_status(path=self.chal)
        self.assertFalse(st["ok"], st)
        self.assertEqual(st["challenges"][0]["status"], "bad_signature")

    def test_cli_issue_and_argument_checks(self):
        p = _run([GATE, "challenge", "1", "3", "--path", self.chal])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = _last_json(p.stdout)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["deadline"], 300.0)
        bad = _run([GATE, "challenge", "5", "2", "--path", self.chal])
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertFalse(_last_json(bad.stdout)["ok"])
        empty = _run([GATE, "challenge-status", "--path",
                      os.path.join(self.state, "no_such_challenges.jsonl")])
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        unknown = _run([GATE, "challenge-status", "ch-0-nope", "--path", self.chal])
        self.assertEqual(unknown.returncode, 1, unknown.stdout + unknown.stderr)


if __name__ == "__main__":
    unittest.main()
