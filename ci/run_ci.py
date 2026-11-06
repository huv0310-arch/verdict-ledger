#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 CI：py_compile + selftest + 沙箱冒烟 + 去业务扫描。全绿 exit 0，任一红 exit 1。

不联网、不写任何既有账：所有账都落在本次运行新建的临时目录里。
"""
import json
import os
import re
import subprocess
import sys
import py_compile
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
FAILS = []

# 去业务扫描口径：公司/个人标识、本机路径、行业词、内部代号。
# 排除：ci/ 自身（含这份词表）、LICENSE（版权人是发布要求）、.git。
DEBIZ_WORDS_B64 = "5rS+5Y2V5p2/fOiKseeUn3zmmZPmmZN85bCP5rS+fOmYv+eIqnzkuIfnv5R8RE1TfOa9jeaftHzpmZXmsb185ZCJ5YipfOino+aUvueJjHzovabniYx86L2u6IOOfOaKpeWNlXzkuInljIV8QWRtaW5pc3RyYXRvcnxDOi9Vc2Vyc3xDOlxcVXNlcnN8RDovVXNlcnN8RDpcXFVzZXJzfOa0vuWNlXzogIHmnb985oC85oC86IOhfOiAgeWphnzmmpflj7d85ouN5p2/fEQtMDEzfEQtMDEyfOWbnuato+mXqHzlj43lsITnuqd857u05oqk6ICF44CMfOe7tOaKpOiAheS7pHznu7TmiqTogIUgMHzlrprmoYh85Lik6YCa6YGTfOS7u+WKoeS5pnwxNToxeHwxODo0eHzliY3pl6ggVjJ85YiA4pGh"
import base64 as _b64
_DEBIZ_CORE = _b64.b64decode(DEBIZ_WORDS_B64).decode("utf-8").split("|")   # 路径/人名/代号类
# 措辞类黑话（发布前第二刀补）：只扫源码的注释行与 docstring 行、以及文档文件的文本行；
# 不进 pyc 字节扫描——pyc 里只剩代码字符串与功能 token（第 15 条豁免的过滤正则一行不动）。
_DEBIZ_WORDING = ("维护者|前门|骨架|账尾|翻书|设计稿|九概念|怼怼|阿爪|小派|悦悦|"
                  "派单板|staging|chitchat_fast").split("|")
DEBIZ_WORDS_PYC = list(_DEBIZ_CORE)
DEBIZ = re.compile("|".join(_DEBIZ_CORE + _DEBIZ_WORDING))
DEBIZ_SKIP_DIRS = {".git", "ci", "__pycache__", ".github"}
# 逐字抄账的证据包（行内 sha256 按原件算，改一个字复算就断）：内容是账本原文、不是本仓库措辞 → 不入扫描
DEBIZ_SKIP_FILES = {"docs/RECASE_EVIDENCE.md"}


def sh(args, env=None, timeout=300, cwd=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(args, cwd=cwd or ROOT, env=e,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def step(name, fn):
    try:
        detail = fn()
        print("PASS  %s%s" % (name, " | " + str(detail) if detail else ""))
    except Exception as exc:
        print("FAIL  %s | %s" % (name, str(exc)[:600]))
        FAILS.append(name)


def tmp_home():
    return tempfile.mkdtemp(prefix="cihome_")


def py_files():
    out = []
    for sub in ("V2", "V3"):
        for dirpath, dirs, files in os.walk(os.path.join(ROOT, sub)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            out += [os.path.join(dirpath, f) for f in files if f.endswith(".py")]
    return sorted(out)


def step_compile():
    # dfile=相对路径：绝对路径会把本机目录名写进 pyc 的 co_filename（编译产物也是发布面）
    files = py_files()
    for f in files:
        py_compile.compile(f, cfile=None, dfile=os.path.relpath(f, ROOT).replace(os.sep, "/"),
                           doraise=True)
    return "%d files (co_filename relative)" % len(files)


def step_borrow_selftest():
    home = tmp_home()
    rc, out = sh([PY, os.path.join("V2", "v2_borrow.py")],
                 env={"USERPROFILE": home,
                      "V2_STATE_DIR": os.path.join(home, "state")})
    if rc != 0 or '"ok"' not in out or "true" not in out:
        raise RuntimeError(out[-800:])
    return "临时账自检 ok"


def step_mcp_selftest():
    # 只给临时 USERPROFILE：本自检自己会把 USERPROFILE 重定向进临时账，
    # 若外层再设 V2_STATE_DIR 会盖住重定向，导致「STATE_DIR 已重定向」那条判红。
    home = tmp_home()
    rc, out = sh([PY, os.path.join("V2", "v2_mcp_server.py"), "--selftest"],
                 env={"USERPROFILE": home},
                 timeout=300)
    if rc != 0:
        raise RuntimeError(out[-1200:])
    return "端到端自测 16 项全过"


def step_speak_selftest():
    rc, out = sh([PY, os.path.join("V3", "ganglion_speak.py"), "--selftest"],
                 env={"SPEAK_DATA_DIR": tmp_home()})
    if rc != 0 or "PASS" not in out:
        raise RuntimeError(out[-800:])
    return out.strip().splitlines()[-1][:60]


def step_sandbox_smoke():
    """沙箱冒烟：记一笔 + 借回同一笔，账只落临时目录。"""
    home = tmp_home()
    state = os.path.join(home, "state")
    code = (
        "import os,sys,json;"
        "sys.path.insert(0, os.path.join(os.getcwd(),'V2'));"
        "import v2_gate as G, v2_borrow as B;"
        "ok=G.record('smoke','示例问法一条',1,'smoke');"
        "k=G._key('smoke','示例问法一条');"
        "h=B.borrow(k,msg='示例问法一条');"
        "r={'record':ok,'borrowed':bool(h.get('borrowed')),"
        "'verdict':(h.get('node') or {}).get('verdict'),'state':G.STATE_DIR};"
        "print(json.dumps(r,ensure_ascii=False));"
        "assert ok and r['borrowed'] and r['verdict']==1, r"
    )
    rc, out = sh([PY, "-c", code],
                 env={"USERPROFILE": home, "V2_STATE_DIR": state})
    if rc != 0:
        raise RuntimeError(out[-800:])
    return json.loads(out.strip().splitlines()[-1])["state"]


def step_selfcheck():
    home = tmp_home()
    env = {
        "USERPROFILE": home,
        "V2_STATE_DIR": os.path.join(home, "state"),
        "V2_JOURNAL": os.path.join(home, "no_journal.jsonl"),
        "V3_SCRIPTS_DIR": os.path.join(ROOT, "V2"),
        "V3_ITER_LEDGER": os.path.join(ROOT, "V3", "iter_template.jsonl"),
        "V3_PUBLIC_LEDGER": os.path.join(home, "no_public.jsonl"),
        "HERMES_CONFIG": os.path.join(home, "no_config.yaml"),
    }
    rc, out = sh([PY, os.path.join("V3", "v3_selfcheck.py")], env=env)
    if rc != 0 or "HAS FAIL" in out:
        raise RuntimeError(out[-1000:])
    return out.strip().splitlines()[-1][:60]


def _src_doc_lines(fp):
    """源码只扫「注释行 + docstring 行」的行号集合：代码行与普通字符串不进扫描
    （第 15 条豁免的过滤正则在代码行里，因此天然不中）。
    解析失败 → 退化为只扫注释行：宁可少扫，不误杀。"""
    import ast
    import io as _io
    import tokenize as _tk
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return set()
    out = set()
    try:
        for t in _tk.generate_tokens(_io.StringIO(src).readline):
            if t.type == _tk.COMMENT:
                out.update(range(t.start[0], t.end[0] + 1))
    except Exception:
        pass
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.update(range(body[0].lineno,
                                 (body[0].end_lineno or body[0].lineno) + 1))
    return out


def step_debiz():
    hits = []
    for sub in ("V2", "V3", "docs", "."):
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in DEBIZ_SKIP_DIRS]
            for fn in files:
                if not fn.endswith((".py", ".ps1", ".md", ".json", ".jsonl", ".yml", ".yaml", ".txt")):
                    continue
                if sub == "." and os.path.dirname(dirpath) != ROOT and os.path.abspath(dirpath) != ROOT:
                    continue
                fp = os.path.join(dirpath, fn)
                if os.path.abspath(fp).startswith(os.path.join(ROOT, "ci")):
                    continue
                if fn == "LICENSE":
                    continue
                rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                if rel in DEBIZ_SKIP_FILES:
                    continue
                want = _src_doc_lines(fp) if fn.endswith(".py") else None
                skip_next = False          # 第 15 条豁免：过滤注释行 + 紧随其后的正则行
                try:
                    with open(fp, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if skip_next:
                                skip_next = False
                                continue
                            if "过滤宿主平台" in line:
                                skip_next = True
                                continue
                            if want is not None and i not in want:
                                continue
                            m = DEBIZ.search(line)
                            if m:
                                hits.append("%s:%d:%s" % (rel, i, m.group(0)))
                except OSError:
                    pass
    # 编译产物也是发布面：__pycache__/*.pyc 按字节搜（docstring 与路径都在里面）。
    # 只搜核心词表（路径/人名/代号）：措辞类词在 pyc 里只剩代码字符串与功能 token，不进扫描。
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", "ci"}]
        for fn in files:
            if fn.endswith(".pyc"):
                fp = os.path.join(dirpath, fn)
                try:
                    raw = open(fp, "rb").read()
                except Exception:
                    continue
                for w in DEBIZ_WORDS_PYC:
                    if w.encode("utf-8") in raw:
                        hits.append("pyc字节:%s->%s" % (os.path.relpath(fp, ROOT), w))
    if hits:
        for h in hits:                     # 有命中：逐条打印
            print("HIT  debiz | " + h)
        raise RuntimeError("业务残留 %d 处" % len(hits))
    return "0 处业务残留"


for _name, _fn in [
    ("py_compile", step_compile),
    ("v2_borrow selftest", step_borrow_selftest),
    ("v2_mcp_server selftest", step_mcp_selftest),
    ("ganglion_speak selftest", step_speak_selftest),
    ("sandbox smoke (record+borrow)", step_sandbox_smoke),
    ("v3_selfcheck", step_selfcheck),
    ("debiz scan", step_debiz),
]:
    step(_name, _fn)

# 发布目录零缓存：清 __pycache__（打包口径，.gitignore 另管 git）
_cleaned = 0
for _r, _d, _f in os.walk(ROOT):
    if "__pycache__" in _d:
        shutil.rmtree(os.path.join(_r, "__pycache__"), ignore_errors=True)
        _cleaned += 1
print("== " + ("ALL GREEN" if not FAILS else "HAS FAIL: " + ", ".join(FAILS)) + (" | pycache cleaned: %d" % _cleaned if _cleaned else ""))
sys.exit(0 if not FAILS else 1)
