# Judgment Ledger (V2 Judgment Account — "Plexus" / V3 Improvement Account — "Contra")

> **Logs tell you what crashed; we tell you what was judged wrong. CI keeps your code sane — this keeps your judgments accountable.**

**V2 builds a court for every AI judgment; V3 puts a watchdog on "AI changing itself."**

`stdlib only` · `zero dependencies` · `core: zero-config` · `three-line install` · `MIT` · `fail-open, never breaks the caller`

**[中文版 README](README.md)**

---

## Why this exists

AI is moving from *talking* to *acting* — agents call tools, make decisions, issue instructions, and even **rewrite themselves**. Three of the hottest open problems right now all collapse into one question: **why should anyone trust it?**

### 1. AI accountability legislation — "why did it decide that?"

The accountability bills under discussion overseas rest on three requirements: judgments must be **traceable**, results **reviewable**, and changes **on the record**. In most agent systems today, an allow/block decision lives in a log for 24 hours — when regulators come knocking, you have no evidence to hand over.

### 2. Agent security — every allow & block in the dark

Every step an agent takes (allow / block / tool call) is still a black box: no audit trail after an incident, no record of which permission was granted to which tool or why. Security circles have a saying: **you can't secure what you can't see.**

### 3. Control of self-improvement — "it changed itself; who noticed?"

A system that rewrites its own code and rules doesn't fear change — it fears **broken changes nobody sees**: no round numbers, no rationale, no rollback reference. The black box upgrades from *one judgment* to *the entire rulebook*. "Controlling recursive self-improvement" is considered one of the hardest open problems in the field, and most proposed answers still live in papers.

**A black box proves *nothing was tampered with*; it cannot prove *the judgment was right*. An alert tells you *it crashed*; it never tells you *it judged wrong*.**
This repository turns those requirements into **running software**: judgments have accounts, reversals leave stamps, improvements have accounts too — pure stdlib, `git clone`, three lines to a working run.

---

## V2 Judgment Account ("Plexus"): every verdict, on the record

**The big question it answers: why trust an AI judgment?**

> **A black box proves nothing was tampered with; we prove the judgment was right — and if it was wrong, it can be reversed.**
> You can only correct what you remember making — **start from the account, not from the alert.**

### Three core capabilities

- **Judged right** — every judgment is one record: value (allow / block / unknown), verdict kind, situation fingerprint, timestamp; **append-only**. Think bank ledgers: errors are corrected by a red-ink reversal, the original voucher never disappears.
- **Wrong judgments can be reversed** — a reversal is an append: the new verdict stamps over the old one, history is never deleted, **who reversed it and why stay on the record** — "what was corrected" is itself audit evidence.
- **Nothing was tampered with** — the journal rides with a hash-chain shadow file, so edited, truncated, or deleted lines are caught by full recomputation; **since v3.1 the chain head can be anchored**: snapshot the current chain head (line count + sha256 digest) into a separate append-only anchor file, and verification compares the local chain head against it — so a tail cut from **journal and chain together** is still caught. Honest boundary: this defends against unilateral tampering, not collusion that rewrites journal and chain together.

### Chain-head anchoring (new in v3.1)

The shadow chain defends against *edited / deleted / singly-truncated* lines; cut a few lines off the **end of journal and chain together** and the remainder still verifies clean — v3.1 adds the minimal fix:

```bash
python V2/v2_gate.py chain-head                     # current chain head: count + sha256 digest + time (read-only)
python V2/v2_gate.py anchor                         # append the chain head to anchors.jsonl (append-only)
python V2/v2_gate.py verify --against anchors.jsonl # compare local head with the anchor: a truncated tail exits 1
```

- The anchor file lives in the ledger dir by default; `V2_ANCHORS` moves it elsewhere — **kept separately, it actually counts**;
- `anchor` writes only the anchor file, journal and shadow chain stay byte-identical; a missing or empty anchor file makes verification go red — **never fakes a pass**;
- An anchor pins up to its own line: rows after it are covered by a newer anchor and by the shadow chain.

### Timeline: the last N nodes (new in v3.1)

```python
import os
from v2_gate import _Ledger, SNAP, JOURNAL, WRITER

L = (_Ledger.load(SNAP, journal=JOURNAL, writer=WRITER, ttl=None, empty_ok=True)
     if os.path.exists(SNAP) else _Ledger(writer=WRITER, ttl=None))
for n in L.recent(5):            # the last 5 nodes across every address, read-only
    print(n["key"], n["ts"], n["verdict"])
```

Three rules: every node on **every address**; sorted by the node's own clock `ts` descending (ties broken by round descending, then address ascending — the order is deterministic); **tombstones excluded**, faded nodes included, `n <= 0` returns empty, nodes without a clock are skipped.

### Where it's used

- **AI agent behavior audit** — replay the whole decision chain: each judgment carries a situation fingerprint and timestamp; who judged, on what basis, was it ever reversed
- **Content moderation / risk-control second review** — misjudgments are corrected by append; **the correction itself is on the account**; alignment drift becomes detectable and fixable instead of overwritten
- **MCP / tool-call governance** — which permission was granted, why, for which call: tool calls turn from a *chain of trust* into a *chain of evidence*
- **Compliance & accountability evidence** — the hash chain makes edited/truncated/deleted lines detectable afterward, and chain-head anchoring makes a **truncated tail** reconcile too; provides recomputable, verifiable records of *what the AI did*
- **Automation decision trails** — every trip/pass of cron jobs, alerts, pipelines can answer *which step judged, on what basis*
- **Trading-gate verdict records** — every allow/block on entries, exits, and stops is booked; reviews can tell *which one was the rule, which one was the itch*

### It grew out of 105 lines

V2's core is a **105-line generic verdict framework** — stdlib-only, zero business logic, unchanged since it froze; the whole ledger grew out of it, **adding only empty slots for "questions you must answer," never one line of any industry's private business**. That is why it dares call itself generic: fork it and you never have to tear it down first. The 105-line ganglion core lives in the ganglion repo: [github.com/huv0310-arch/ganglion](https://github.com/huv0310-arch/ganglion).

---

## V3 Improvement Account ("Contra"): AI changing itself, on the record too

**The big question it answers: when an AI improves itself, who notices a bad change?**

> While the world debates "will AI go out of control" —
> here is a running ledger of **exactly what it changed**, in production.

One-line version: **Ctrl+Z for AI self-modification** — reversal is undo, the iteration ledger is version history.

A system that rewrites its own code and rules needs not more intelligence but a **mirror discipline**: every round leaves one line — what changed, why, how it went — so **self-modification becomes auditable history** and rollback always has a reference.

### Three pieces

- **Iteration account** — one line per round (what / why / result), never skipped; it answers exactly one question: *is the last change still holding up?*
- **One-shot self-check** — accounts, wiring, standard parts checked item by item; missing parts are recorded as SKIP — **never fake a pass**, the most valuable honesty in this system
- **Spoke-aloud policy** — speak only when something worth saying turns up; silence otherwise; sleep/wake rhythm saves real calls. **Not an agent burning tokens forever — an agent with a pulse.**

### Where it's used

- **Self-improvement control trails** — improvement process reviewable, rollback referenced; *who changed it, in which round* is always retrievable
- **Agent self-healing audit** — every auto bug-fix and parameter tune is booked; *what it touched by itself* is no longer a blind spot
- **Long-running agent resource rhythm** — sleep and wake are booked; cost and activity become explainable — saving money finally has a ledger

---

## How is this different from logs, monitoring, traces?

| | Answers | Survives a reboot | Can say "judged wrong" |
| --- | --- | --- | --- |
| Logs | what happened | rarely | no |
| Monitoring | is it crashing now | no replay | no |
| Traces | how the request flowed | depends on storage | no |
| **This repo** | **was the judgment right, what changed** | **if the account is there, yes** | **yes — reversal on record** |

One line: **logs watch crashes, we watch verdicts; traces record the route, we record the case.**

---

## How the two accounts fit together

- **V2 judgment account** (gate + ledger + read-only borrow + MCP tool table) — what was judged, by whom, was it reversed: owns **right vs. wrong**
- **V3 improvement account** (self-check + spoke-aloud + iteration ledger) — what changed, why, how it went: owns **the system that judges**

**Together they are an agent substrate you can hold accountable: AI at work needs an account, AI changing itself needs one too.**

No database to install, not one line of your existing code touched — `git clone`, three lines to your first verdict, the ledger is yours.

---

## Install (three lines)

```bash
git clone <repo-url> && cd <repo>
python -m py_compile V2/*.py V2/v2_gate/*.py V3/*.py    # stdlib only
python V2/v2_mcp_server.py --selftest                    # sandbox self-test: builds a temp ledger, touches none of your data
```

## Minimal example (fake data)

```python
import os, sys
os.environ.setdefault("V2_STATE_DIR", "./.v2_state")   # ledger dir is configurable; sandbox first
sys.path.insert(0, "V2")

import v2_gate as G
import v2_borrow as B

G.record("demo", "is this spam?", 1, "example verdict")    # book one
key = G._key("demo", "is this spam?")
print(B.borrow(key, msg="is this spam?"))                 # borrow it back
```

After the run, `V2_STATE_DIR` holds `journal.jsonl` (journal) and `ledger.json` (snapshot).

## Layout

```
V2/                     judgment account
  v2_gate.py            verdict gate: book, reverse, chain-verify, chain-head anchoring (chain-head / anchor / verify --against); ledger dir via V2_STATE_DIR
  v2_gate/ledger.py     ledger body: append, borrow, lock, compact, age-out, archive, timeline recent(n)
  v2_borrow.py          read-only borrow: list / quick-scan / single read, never mutates verdicts
  v2_mcp_server.py      MCP tool table (record / borrow), self-test via --selftest
  ctx_fp.py             situation fingerprint & address key formula: both ends must share this file
  v2_mcp_config_example.json   host wiring example (placeholder paths)
  install.ps1           copy + py_compile check + wiring hints
V3/                     improvement account
  v3_selfcheck.py       one-shot self-check: accounts, wiring, standard parts; missing = SKIP, never red-fake
  ganglion_speak.py     spoke-aloud: speak only when worth it, nothing found = silence
  iter_schema.json      iteration account field spec (two record shapes)
  iter_template.jsonl   empty template: one line at a time
docs/                   release SOP & reference stubs
  ANNOUNCE.md           announcement draft (CN+EN, placeholders; not sent as-is)
  RECASE_EVIDENCE.md    recase evidence: one full replay (wrong -> appeal -> chain green -> anchor match)
  RELEASE.md            release SOP: four-gate checklist & pre-release recheck
  纪念章规格_引用位.md   medal spec reference stub (master copy not in repo)
  锚定与发币_立场.md     anchoring & token stance: no-issuance policy, four conventions, payout address, boundaries
payout_address_proof.txt   payout address attestation: address, repo, public fingerprint, 3-step verification
.github/workflows/      CI (runs locally first; not pushed = not public)
ci/run_ci.py            local CI: compile + self-tests + sandbox smoke + acceptance tests + de-biz scan
tests/                  acceptance tests (unittest, stdlib only): chain-head anchoring / recent(n) timeline / torn-tail healing
```

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `V2_STATE_DIR` | V2 ledger dir (journal / snapshot / archive / lock) | `%USERPROFILE%\.hermes\state\v2` |
| `V2_WRITER` | writer identity for booked entries | `v2-front-door` (borrow side: `v2-borrow`) |
| `V2_ANCHORS` | chain-head anchor file path (append-only, what `verify --against` compares) | `anchors.jsonl` in the ledger dir |
| `V3_SCRIPTS_DIR` | where the self-check looks for standard parts | `%LOCALAPPDATA%\hermes\scripts` |
| `HERMES_CONFIG` | host config path (wiring probe, missing = SKIP) | `%LOCALAPPDATA%\hermes\config.yaml` |
| `V2_JOURNAL` / `V3_PUBLIC_LEDGER` / `V3_ITER_LEDGER` | explicit paths for the three ledgers, missing = SKIP | public/iteration near `data/`; verdict journal in `V2_STATE_DIR` |
| `V3_PUBLIC_MIN` | minimum rows for the public ledger | `1` |
| `SPEAK_DATA_DIR` | spoke-aloud state dir | `~/.speak_state` |
| `SPEAK_LLM_URL` / `SPEAK_LLM_MODEL` / `LLM_API_KEY` | model endpoint / name / key | no endpoint = no model call, rule-based fallback |

> In the table above, `Hermes` = the open-source host runtime (github.com/NousResearch/hermes-agent); all these paths are configurable.

## Boundaries (honest)

- **fail-open**: broken ledger, broken lock, unreadable account — the caller never crashes; behavior falls back to the account-less status quo.
- **Optional coupling**: `ledger.py`'s `addr()` and `LedgerGanglion` need an external reflection standard part (separate repo); the core ledger works without it — record and borrow just fine.
- **No network by default**: the spoke-aloud piece only calls a model when endpoint and key are explicitly configured; keys read from environment variables only — never stored, never echoed.
- **Three ledgers**: verdict journal `journal.jsonl`, snapshot `ledger.json`, expired archive `expired.jsonl`, all under `V2_STATE_DIR`.
- **Chain-head anchoring (since v3.1)**: `anchor` appends the chain head (count + sha256 digest + time) to a **local anchor file** `anchors.jsonl` (same dir by default; `V2_ANCHORS` moves it somewhere separate, which is the useful setup), and `verify --against` compares the local head against recorded anchors to catch a **truncated tail**.
  **Honest boundary**: external third-party timestamp anchoring (publishing the chain head off-box / to a third party) **is not done** — that is the layer you need when the account is used as evidence; journal + chain + anchor rewritten together on one disk is still outside what this defends.

## Acknowledgement

This project was developed end to end with the mimo model — thanks to the model team.
It needs no advertising: this repository is its benchmark.

## Release

Current release tag: **v3.1** (version lives in `V2/v2_gate.py` `__version__`; both READMEs and docs stay in sync).
Before any public push, walk the four-gate checklist and announcement template in `docs/RELEASE.md`; if the checklist isn't fully ticked, nothing ships.
