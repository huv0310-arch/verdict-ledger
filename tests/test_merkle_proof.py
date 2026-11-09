"""不给行也能验的一致性证明（Merkle，RFC 9162）验收。

账目录只在本次运行新建的临时目录里开 —— 已有的账一个字节不碰；
签名密钥只在本进程内生成一把测试钥，仓里不放任何真密钥。
"""
import hashlib
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

_STATE = tempfile.mkdtemp(prefix="merkle_proof_")
os.environ.setdefault("V2_STATE_DIR", _STATE)
os.environ.pop("V2_ANCHORS", None)
os.environ.pop("V2_MERKLE", None)
os.environ.pop("V2_CHALLENGES", None)
os.environ.setdefault("V2_ANCHOR_KEY", secrets.token_hex(16))
sys.path.insert(0, os.path.join(ROOT, "V2"))
import v2_gate as G  # noqa: E402

MARKS = "甲乙丙丁戊己庚辛壬癸"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _rows(p):
    return [json.loads(x) for x in _read(p).splitlines() if x.strip()]


def _last_json(out):
    return json.loads(out.strip().splitlines()[-1])


def _run(argv):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable] + argv, cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


def _naive_root(rows):
    """独立写的一份 RFC 9162 §2.1.1 树哈希（切片式），不复用被测实现，用来对撞。"""
    lv = [hashlib.sha256(b"\x00" + G._canon(r).encode("utf-8")).hexdigest() for r in rows]

    def m(lo, hi):
        n = hi - lo
        if n == 0:
            return hashlib.sha256(b"").hexdigest()
        if n == 1:
            return lv[lo]
        k = 1 << (((n - 1).bit_length()) - 1)
        left = m(lo, lo + k)
        right = m(lo + k, hi)
        return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()

    return m(0, len(lv))


class Base(unittest.TestCase):
    def setUp(self):
        for fn in ("journal.jsonl", "journal.jsonl.chain", "ledger.json",
                   "anchors.jsonl", "challenges.jsonl", "merkle.jsonl"):
            p = os.path.join(G.STATE_DIR, fn)
            if os.path.exists(p):
                os.remove(p)
        self.state = G.STATE_DIR
        self.journal = os.path.join(self.state, "journal.jsonl")
        self.chain = self.journal + ".chain"
        self.anchors = os.path.join(self.state, "anchors.jsonl")
        self.merkle = os.path.join(self.state, "merkle.jsonl")
        self.chal = os.path.join(self.state, "challenges.jsonl")
        os.environ.pop("V2_PROOF_DEADLINE", None)

    def grow(self, n):
        """把账记到 n 笔（只增不改）。"""
        for i in range(len(G._journal_rows()), n):
            self.assertTrue(G.record("demo", "示例问法" + MARKS[i], 1, "kind"))

    def keyset(self):
        key, kid, all_keys, err = G._load_keys()
        self.assertEqual(err, "", err)
        self.assertIsNotNone(key)
        return key, kid, all_keys


class MerkleTreeCase(Base):
    """树形与证明本体：先证明算法本身站得住，再谈接线。"""

    def test_merkle_root_matches_an_independent_implementation(self):
        self.grow(6)
        rows = G._journal_rows()
        self.assertEqual(G.merkle_root(rows), _naive_root(rows))
        self.assertEqual(G.merkle_root([]), hashlib.sha256(b"").hexdigest())

    def test_merkle_root_moves_when_a_row_moves(self):
        self.grow(4)
        rows = G._journal_rows()
        base = G.merkle_root(rows)
        rows2 = json.loads(json.dumps(rows))
        rows2[1]["node"]["verdict"] = 0
        self.assertNotEqual(base, G.merkle_root(rows2), "改一行，根必须跟着变")

    def test_rfc9162_seven_leaf_example_is_c_d_g_l(self):
        """RFC 9162 §2.1.5：PROOF(3, D[7]) = [c, d, g, l]，逐个对上。"""
        rfc_rows = ["d%d" % i for i in range(7)]
        lv = [hashlib.sha256(b"\x00" + G._canon(r).encode("utf-8")).hexdigest()
              for r in rfc_rows]

        def h(a, b):
            return hashlib.sha256(b"\x01" + bytes.fromhex(a) + bytes.fromhex(b)).hexdigest()

        g = h(lv[0], lv[1])
        hnode = h(lv[2], lv[3])
        inode = h(lv[4], lv[5])
        jnode = lv[6]
        lnode = h(inode, jnode)
        nodes, err = G.merkle_consistency_proof(rfc_rows, 3, 7)
        self.assertEqual(err, "", err)
        self.assertEqual(nodes, [lv[2], lv[3], g, lnode])
        ok, why = G.check_merkle_consistency(3, 7, _naive_root(rfc_rows[:3]),
                                             _naive_root(rfc_rows), nodes)
        self.assertTrue(ok, why)

    def test_every_prefix_pair_up_to_24_verifies(self):
        for n in range(2, 25):
            sample = [{"i": k} for k in range(n)]
            roots = [_naive_root(sample[:c]) for c in range(n + 1)]
            for m in range(1, n):
                nodes, err = G.merkle_consistency_proof(sample, m, n)
                self.assertEqual(err, "", (m, n, err))
                ok, why = G.check_merkle_consistency(m, n, roots[m], roots[n], nodes)
                self.assertTrue(ok, (m, n, why))

    def test_tampered_or_truncated_nodes_go_red(self):
        sample = [{"i": k} for k in range(10)]
        roots = [_naive_root(sample[:c]) for c in range(11)]
        nodes, err = G.merkle_consistency_proof(sample, 4, 10)
        self.assertEqual(err, "", err)
        bad = list(nodes)
        bad[0] = "0" * 64
        self.assertFalse(G.check_merkle_consistency(4, 10, roots[4], roots[10], bad)[0])
        self.assertFalse(G.check_merkle_consistency(4, 10, roots[4], roots[10],
                                                    nodes[:-1])[0], "截掉一个节点必须判红")
        self.assertFalse(G.check_merkle_consistency(4, 10, roots[10], roots[4], nodes)[0],
                         "两枚根对调必须判红")
        self.assertFalse(G.check_merkle_consistency(4, 10, roots[4], roots[10], [])[0],
                         "空证明必须判红")
        self.assertFalse(G.check_merkle_consistency(4, 10, roots[4], roots[10] + "ff", nodes)[0],
                         "根不是 64 位必须判红")


class MerkleAnchorCase(Base):
    """Merkle 锚：与四字段锚同时签发、绑定同一条数同一根，四字段锚一个字节不改。"""

    def test_anchor_also_signs_a_merkle_record_and_keeps_the_four_field_row_intact(self):
        self.grow(3)
        a = G.anchor(path=self.anchors)
        self.assertTrue(a["ok"], a)
        self.assertTrue(a.get("merkle_ok"), a)
        row = _rows(self.anchors)[0]
        self.assertEqual(set(row), {"v", "algo", "entry_count", "root_hash",
                                    "issued_at", "key_id", "sig"},
                         "四字段锚的行格式不许被 Merkle 挤占")
        mrows = _rows(self.merkle)
        self.assertEqual(len(mrows), 1)
        rec = mrows[0]
        self.assertEqual(set(rec), {"v", "algo", "entry_count", "root_hash",
                                    "merkle_root", "issued_at", "key_id", "sig"})
        self.assertEqual(rec["entry_count"], 3)
        self.assertEqual(rec["root_hash"], row["root_hash"], "Merkle 锚必须与四字段锚同根")
        self.assertEqual(rec["merkle_root"], G.chain_head()["merkle_root"])
        key, kid, all_keys = self.keyset()
        self.assertTrue(G._sig_ok(G._merkle_payload(rec), rec["sig"], all_keys),
                        "Merkle 锚必须签得过")

    def test_chain_head_carries_a_merkle_root_and_stays_readonly(self):
        self.grow(4)
        before = (os.path.getsize(self.journal), os.path.getsize(self.chain))
        h = G.chain_head()
        self.assertTrue(h["ok"], h)
        self.assertTrue(h["merkle_root"].startswith("sha256:"), h)
        self.assertEqual(h["merkle_root"], "sha256:" + G.merkle_root(G._journal_rows()))
        self.assertEqual(before, (os.path.getsize(self.journal), os.path.getsize(self.chain)),
                         "chain_head 只读：账与影子链一个字节都不许动")

    def test_a_tampered_merkle_record_fails_the_signature_check(self):
        self.grow(3)
        G.anchor(path=self.anchors)
        rec = _rows(self.merkle)[0]
        rec["merkle_root"] = "sha256:" + "0" * 64
        _write(self.merkle, json.dumps(rec, ensure_ascii=False) + "\n")
        bound, err = G._merkle_binding(3, self.anchors, self.merkle)
        self.assertIsNone(bound)
        self.assertIn("签名核不过", err)

    def test_a_merkle_record_bound_to_another_root_is_not_accepted(self):
        self.grow(3)
        G.anchor(path=self.anchors)
        rec = _rows(self.merkle)[0]
        rec["root_hash"] = "sha256:" + "f" * 64          # 与四字段锚不同根，签名照样对不上也不采信
        _write(self.merkle, json.dumps(rec, ensure_ascii=False) + "\n")
        bound, err = G._merkle_binding(3, self.anchors, self.merkle)
        self.assertIsNone(bound)
        self.assertIn("没有第 3 条", err)


class MerkleResponseCase(Base):
    """挑战单的 Merkle 答复：不给行、复算过、缺锚如实报错。"""

    def _anchored_pair(self, frm=2, to=6):
        self.grow(frm)
        self.assertTrue(G.anchor(path=self.anchors)["ok"])
        self.grow(to)
        self.assertTrue(G.anchor(path=self.anchors)["ok"])

    def test_merkle_only_response_carries_no_rows_and_verifies_end_to_end(self):
        self._anchored_pair()
        c = G.challenge(2, 6, path=self.chal)
        self.assertTrue(c["ok"], c)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        r = G.challenge_respond(c["request_id"], path=self.chal, merkle_only=True)
        self.assertTrue(r["ok"], r)
        proof = r["response"]
        self.assertEqual(proof["algo"], "sha256-merkle")
        self.assertIn("nodes", proof)
        self.assertNotIn("rows", proof, "Merkle 答复一个原始行都不许带")
        self.assertLess(len(proof["nodes"]), 8, "节点数是 O(log n)，不许把整本账交出去")
        ok, why = G.check_proof(proof)
        self.assertTrue(ok, why)
        st = G.challenge_status(c["request_id"], path=self.chal)
        row = st["challenges"][0]
        self.assertEqual(row["status"], "answered")
        self.assertTrue(row["proof_ok"], row)
        self.assertTrue(st["ok"], st)

    def test_missing_merkle_anchor_refuses_honestly_and_writes_no_response(self):
        self.grow(6)
        self.assertTrue(G.anchor(path=self.anchors)["ok"])     # 只锚了第 6 条
        c = G.challenge(3, 6, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        r = G.challenge_respond(c["request_id"], path=self.chal, merkle_only=True)
        self.assertFalse(r["ok"], r)
        self.assertIn("锚文件里没有第 3 条", r["why"])
        types = [x.get("type") for x in _rows(self.chal)
                 if x.get("request_id") == c["request_id"]]
        self.assertNotIn("response", types, "凑不出证明就不许写答复")
        st = G.challenge_status(c["request_id"], path=self.chal)
        self.assertEqual(st["challenges"][0]["status"], "pending", "缺答复不许判失败")

    def test_rows_response_still_verifies_after_the_merkle_upgrade(self):
        self.grow(6)
        c = G.challenge(3, 6, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        r = G.challenge_respond(c["request_id"], path=self.chal)
        self.assertTrue(r["ok"], r)
        self.assertIn("rows", r["response"], "老形态（带行）必须照旧能答")
        ok, why = G.check_proof(r["response"])
        self.assertTrue(ok, why)
        st = G.challenge_status(c["request_id"], path=self.chal)
        self.assertTrue(st["challenges"][0]["proof_ok"], st)

    def test_a_signed_but_wrong_merkle_proof_is_reported_as_bad_proof(self):
        self._anchored_pair()
        c = G.challenge(2, 6, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        self.assertTrue(G.challenge_respond(c["request_id"], path=self.chal,
                                            merkle_only=True)["ok"])
        rows = _rows(self.chal)
        resp = next(x for x in rows if x.get("type") == "response")
        bad = json.loads(json.dumps(resp["response"]))
        bad["nodes"][0] = "0" * 64                       # 改节点，但把签名重签回去
        key, kid, all_keys = self.keyset()
        payload = {"request_id": resp["request_id"], "response": bad,
                   "responded_at": resp["responded_at"]}
        resp["response"] = bad
        resp["body"] = G._canon(payload)
        resp["signature"] = G._hmac_sign(key, G._canon(payload))
        _write(self.chal, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
        st = G.challenge_status(c["request_id"], path=self.chal)
        row = st["challenges"][0]
        self.assertEqual(row["status"], "bad_proof", row)
        self.assertFalse(st["ok"], st)

    def test_from_zero_is_vacuous_but_still_checks_the_to_anchor(self):
        self._anchored_pair()
        c = G.challenge(0, 6, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        r = G.challenge_respond(c["request_id"], path=self.chal, merkle_only=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["response"]["nodes"], [], "空前缀不带节点")
        st = G.challenge_status(c["request_id"], path=self.chal)
        row = st["challenges"][0]
        self.assertTrue(row["proof_ok"], row)
        self.assertIn("空前缀", row["proof_why"])

    def test_cli_challenge_respond_accepts_merkle_only(self):
        self._anchored_pair()
        c = G.challenge(2, 6, path=self.chal)
        self.assertTrue(G.challenge_deliver(c["request_id"], path=self.chal)["ok"])
        p = _run([GATE, "challenge-respond", c["request_id"], "--merkle-only",
                  "--path", self.chal])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = _last_json(p.stdout)
        self.assertTrue(out["ok"], out)
        self.assertIn("nodes", out["response"])
        self.assertNotIn("rows", out["response"])
        st = G.challenge_status(c["request_id"], path=self.chal)
        row = st["challenges"][0]
        self.assertEqual(row["status"], "answered")
        self.assertTrue(row["proof_ok"], row)


if __name__ == "__main__":
    unittest.main()
