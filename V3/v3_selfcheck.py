# -*- coding: utf-8 -*-
"""v3_selfcheck.py - V3 迭代后一键体检（2026-11-05 第一轮建，无回归）
用法: python v3_selfcheck.py   全绿 exit 0 / 有红 exit 1"""
import hashlib, os, re, subprocess, sys

SC = os.environ.get("V3_SCRIPTS_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or "", "hermes", "scripts")
CFG = os.environ.get("HERMES_CONFIG") or os.path.join(os.environ.get("LOCALAPPDATA") or "", "hermes", "config.yaml")
JOURNAL = os.environ.get("V2_JOURNAL") or os.path.join(
    os.environ.get("USERPROFILE") or "", ".hermes", "state", "v2", "journal.jsonl")
PUBLIC = os.environ.get("V3_PUBLIC_LEDGER") or os.path.join(os.getcwd(), "data", "public_ledger.jsonl")
ITER = os.environ.get("V3_ITER_LEDGER") or os.path.join(os.getcwd(), "data", "v3_iter.jsonl")
PUBLIC_MIN = int(os.environ.get("V3_PUBLIC_MIN") or "1")   # 公共账最少行数（按部署自己定）
OK = True


def chk(name, cond, detail=""):
    global OK
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail else ""))
    if not cond:
        OK = False


# 1) 骨节 sha（骨节标准件在场才查；本库不含骨节本体（另行发布），缺场记 SKIP 不判红）
g = os.path.join(SC, "ganglion.py")
if os.path.exists(g):
    h = hashlib.sha256(open(g, "rb").read()).hexdigest()
    want = (os.environ.get("V3_GANGLION_SHA16") or "")[:16]
    chk("骨sha", (not want) or h.startswith(want), h[:16])
else:
    print("SKIP 骨sha | 骨节标准件不在场；本库不含骨节本体（另行发布）")

# 2) 宿主入口 shell hook 注册 + 脚本存在（宿主配置缺席 = SKIP）
try:
    cfg = open(CFG, encoding="utf-8", errors="replace").read()
except Exception:
    cfg = ""
if not cfg:
    print("SKIP 宿主hook | 宿主配置不在场:", CFG)
else:
    chk("宿主pre_llm_call注册", "pre_llm_call" in cfg and "hooks:" in cfg)
    fm = re.search(r"pre_llm_call:.*?command:\s*python\s+\"([^\"]+)\"", cfg, re.S)
    if fm:
        chk("宿主hook脚本存在", os.path.exists(fm.group(1)), fm.group(1))
    else:
        chk("宿主hook脚本路径解析", False, "config 里没解析到 python 命令")

# 3) 两本账行数
if os.path.exists(JOURNAL):
    n = sum(1 for _ in open(JOURNAL, encoding="utf-8", errors="replace"))
    chk("判决账journal", n > 0, "%d 行" % n)
else:
    print("SKIP 判决账journal | 账不在场:", JOURNAL)
if os.path.exists(PUBLIC):
    n = sum(1 for _ in open(PUBLIC, encoding="utf-8", errors="replace"))
    chk("公共账", n >= PUBLIC_MIN, "%d 行" % n)
else:
    print("SKIP 公共账 | 账不在场:", PUBLIC)

# 4) 标准件自测
_borrow = os.path.join(SC, "v2_borrow.py")
if os.path.exists(_borrow):
    r = subprocess.run([sys.executable, _borrow], capture_output=True, text=True, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    chk("v2_borrow selftest", r.returncode == 0 and '"ok"' in out and "true" in out,
        out.strip().splitlines()[0][:60] if out.strip() else "no output")
else:
    print("SKIP v2_borrow selftest | 标准件不在场:", _borrow)

# 5) V3 迭代账在（沙箱里给空模板即可）
if os.path.exists(ITER):
    chk("V3迭代账", True, ITER)
else:
    print("SKIP V3迭代账 | 账不在场:", ITER)

print("== " + ("ALL GREEN" if OK else "HAS FAIL"))
sys.exit(0 if OK else 1)
