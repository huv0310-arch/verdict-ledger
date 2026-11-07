#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_mcp_server.py —— V2 标准件 MCP 工具档（2026-11-05）

给宿主（Hermes＝开源宿主运行时名 github.com/NousResearch/hermes-agent，路径可配；任何 MCP 客户端）的工具表挂两把钥匙，不碰用户代码：
    v2_record(content, verdict, reason, platform)  -> 调 v2_gate.record 记账
    v2_borrow(content, platform)                  -> 调 v2_borrow.borrow 借账（带 msg 语义借账）

纯 stdlib：手写 JSON-RPC 2.0 over stdio（MCP stdio 传输 = 换行分隔 JSON），不依赖 mcp SDK。
key/指纹公式一行不改：走 v2_gate._key() 与 ctx_fp.make() 现行版（现行公式）。
fail-open：工具内任何异常都变成 isError 返回，服务器主循环不因单次调用崩。

用法:
    python v2_mcp_server.py              # stdio MCP server（由宿主拉起）
    python v2_mcp_server.py --selftest   # 临时账端到端自测（不碰真账）
    python v2_mcp_server.py --version

安装（≤10 行）:
    1. 把 v2_mcp_server.py 拷到 v2_gate.py / v2_borrow.py / ctx_fp.py / v2_gate/ledger.py 同一目录。
    2. python -m py_compile v2_mcp_server.py && python v2_mcp_server.py --selftest 全绿。
    3. 宿主 config.yaml 的 mcp_servers 段加一条（样例见 v2_mcp_config_example.json）。
    4. 重启宿主 -> 工具表出现 mcp_<服务名>_v2_record / mcp_<服务名>_v2_borrow。
    5. 卸载 = 删该配置条目（宿主 config.yaml 全程只加这一条），账本不动。
    6. 宿主若不透传 USERPROFILE，给条目加 env.USERPROFILE 指向家目录
       （账落 %USERPROFILE%\\.hermes\\state\\v2\\journal.jsonl + ledger.json）。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SERVER_NAME = "v2"                                # 宿主工具名前缀: mcp_v2_v2_record / mcp_v2_v2_borrow
SERVER_VERSION = "3.2"                            # 与 v2_gate.__version__ 同步
DEFAULT_PROTOCOL = "2025-11-25"                   # 客户端报了不认识的版本时的兜底（握手集最新）
HANDSHAKE_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

_G = _B = _C = None                               # 惰性 import：selftest 先重定向 USERPROFILE 再拉账本


class ToolError(Exception):
    """参数/判决语义错误 -> isError 返回，不进 JSON-RPC error。"""


def _mods():
    """首次调用才 import（保证 selftest 的临时账重定向先生效）。"""
    global _G, _B, _C
    if _G is None:
        import v2_gate as G            # noqa: N813  STATE_DIR/JOURNAL/SNAP 按当前 USERPROFILE 定
        import v2_borrow as B          # noqa: N813
        import ctx_fp as C             # noqa: N813
        _G, _B, _C = G, B, C
    return _G, _B, _C


# ══════════════════ 工具描述（0/1/None 语义写清）══════════════════

REC_DESC = (
    "记一条 V2 骨节判决入账（ganglion 标准件账本，fail-open 不抛）。"
    "verdict 语义: 0=放行(进常规流程), 1=拦截(走快答通道); "
    "null(None)=判决未结成 -> 拒绝入账并返回 isError（本工具只收整数 0/1，不写悬位）。"
    "reason=判决种类(如 quick_reply / batch_review)，取最后一个词入账。"
    "platform 参与情形指纹 ctx_fp: 同一句在不同平台是不同情形、配不上就借不到(防串门)；"
    "地址 key 沿用 v2_gate 现行公式(清洗后内容哈希)，platform 不改 key。"
    "返回 {ok, key, verdict, kind, platform, ctx_fp}；账本写入失败 -> ok=false。"
)
BOR_DESC = (
    "按 content+platform 翻 V2 账借旧判决（原 key miss 再走语义借账折候选，带 msg 借账）。"
    "返回 verdict/reason/why_not 语义: 借到(borrowed=true)时 verdict=0|1"
    "(0=上次判放行, 1=上次判拦截)，reason=账上原判决串，why_not=null; "
    "没借到时 verdict=null、reason=null，why_not= "
    "no_record(没记过) | expired(出窗) | mismatch(指纹配不上) | too_fresh(刚判完)。"
    "borrowed=true 才算借到；账本异常 fail-open -> ok=false。"
)

REC_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "要记账的原文（消息内容）"},
        "verdict": {
            "type": ["integer", "null"],
            "description": "0=放行, 1=拦截; null=判决未结成, 拒绝入账(isError)",
        },
        "reason": {"type": "string", "description": "判决种类，如 quick_reply（单个词最稳）"},
        "platform": {"type": "string", "description": "通道名，如 desktop / cli / cron / subagent"},
    },
    "required": ["content", "verdict", "platform"],
}
BOR_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "要借判的原文（消息内容）"},
        "platform": {"type": "string", "description": "通道名（进情形指纹 ctx_fp，须与记账时一致才配得上）"},
    },
    "required": ["content", "platform"],
}

TOOLS = [
    {"name": "v2_record", "description": REC_DESC, "inputSchema": REC_SCHEMA},
    {"name": "v2_borrow", "description": BOR_DESC, "inputSchema": BOR_SCHEMA},
]


# ══════════════════════════ 两把钥匙 ══════════════════════════

def tool_record(a):
    content = a.get("content")
    platform = a.get("platform")
    verdict = a.get("verdict")
    reason = a.get("reason") or ""

    if not isinstance(content, str) or not content.strip():
        raise ToolError("content 必填: 要记账的原文")
    if not isinstance(platform, str) or not platform:
        raise ToolError("platform 必填: 通道名(desktop/cli/cron/...)")
    if verdict is None:
        raise ToolError("verdict=null: 判决未结成，v2_record 只收 0(放行)/1(拦截)，不入账")
    if type(verdict) is not int or verdict not in (0, 1):
        raise ToolError(f"verdict 只能是整数 0 或 1（收到 {verdict!r}），字符串 '0'/'1' 不收")

    G, _B, C = _mods()
    # kind 取 reason 最后一个词 —— 与调用方借账时 reason.split()[-1] 的解析方向一致
    toks = reason.strip().split()
    kind = toks[-1] if toks else "mcp_record"
    ctx = C.make(platform, content)               # 情形指纹（现行公式，platform 在这里进账）
    key = G._key(platform, content)               # 地址 key（现行公式，一行不改）
    ok = G.record(platform, content, verdict, kind, ctx_fp=ctx)
    out = {"ok": bool(ok), "key": key, "verdict": verdict, "kind": kind,
           "platform": platform, "ctx_fp": ctx}
    if not ok:
        out["error"] = "账本写入失败（ledger 缺失或异常），fail-open 不抛"
    return out


def tool_borrow(a):
    content = a.get("content")
    platform = a.get("platform")
    if not isinstance(content, str) or not content.strip():
        raise ToolError("content 必填: 要借判的原文")
    if not isinstance(platform, str) or not platform:
        raise ToolError("platform 必填: 通道名（进 ctx_fp，须与记账时一致）")

    G, B, C = _mods()
    key = G._key(platform, content)
    ctx = C.make(platform, content)
    r = B.borrow(key, ctx_fp=ctx, msg=content)    # 原 key miss 才走语义借账候选（路径 D）
    if not r.get("ok"):
        return {"ok": False, "key": key, "error": r.get("error")}
    node = r.get("node") or {}
    hit = bool(r.get("borrowed"))
    out = {
        "ok": True,
        "borrowed": hit,
        "key": key,
        "verdict": node.get("verdict") if hit else None,     # 借到=0/1；没借到=None
        "reason": node.get("reason") if hit else None,       # 借到=原判决串；没借到=None
        "why_not": r.get("why_not"),                         # 借到=None；没借到=no_record/expired/mismatch/too_fresh
        "ms": r.get("ms"),
    }
    if r.get("via"):
        out["via"] = r["via"]
    return out


# ══════════════════════════ JSON-RPC 2.0 over stdio ══════════════════════════

def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(mid, result):
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid, code, message):
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def _tool_text(payload, is_error):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": bool(is_error)}


def _handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if is_notification:                          # initialized / cancelled / ... 一律不回
        return

    try:
        if method == "initialize":
            req = params.get("protocolVersion")
            proto = req if req in HANDSHAKE_PROTOCOLS else DEFAULT_PROTOCOL
            _result(mid, {
                "protocolVersion": proto,        # 回显客户端要的版本（须在握手集内，客户端校验）
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "ping":
            _result(mid, {})
        elif method == "tools/list":
            _result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "v2_record":
                    payload = tool_record(args)
                    is_err = not payload.get("ok", False)
                elif name == "v2_borrow":
                    payload = tool_borrow(args)
                    is_err = not payload.get("ok", False)
                else:
                    _error(mid, -32602, f"Unknown tool: {name}")
                    return
            except ToolError as e:
                _result(mid, _tool_text({"ok": False, "error": str(e)}, True))
                return
            except Exception as e:               # fail-open: 单次调用炸了不带走服务器
                _result(mid, _tool_text({"ok": False, "error": f"{type(e).__name__}: {e}"}, True))
                return
            _result(mid, _tool_text(payload, is_err))
        else:
            _error(mid, -32601, f"Method not found: {method}")
    except Exception as e:                       # 兜底：绝不能让主循环死
        try:
            _error(mid if mid is not None else None, -32603, f"{type(e).__name__}: {e}")
        except Exception:
            pass


def serve():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")     # Windows 默认 locale 编码会啃中文 JSON
        except Exception:
            pass
    sys.stderr.write(f"v2_mcp_server v{SERVER_VERSION} stdio ready (dir={HERE})\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _error(None, -32700, "Parse error")
            continue
        if isinstance(msg, dict):
            _handle(msg)


# ══════════════════════════ selftest（临时账，不碰真账）══════════════════════════

def selftest():
    import py_compile
    import queue
    import shutil
    import subprocess
    import tempfile
    import threading

    checks = []

    def ck(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(("PASS " if ok else "FAIL ") + name + ((" -- " + str(detail)) if detail else ""),
              flush=True)

    # ① 自己能编译（dfile 相对路径：绝对路径会把本机目录名写进 pyc 的 co_filename，
    #    编译产物也是发布面 —— 与 ci/run_ci.py 的 step_compile 同一口径）
    try:
        py_compile.compile(os.path.abspath(__file__), doraise=True,
                           dfile=os.path.relpath(os.path.abspath(__file__),
                                                 os.path.dirname(HERE)).replace(os.sep, "/"))
        ck("py_compile self", True)
    except Exception as e:
        ck("py_compile self", False, e)

    # 真账只做「前后字节对比」的软观察（调用方可能并发写，不作硬判定）
    real_up = os.environ.get("USERPROFILE", "")
    real_state = os.environ.get("V2_STATE_DIR") or os.path.join(real_up, ".hermes", "state", "v2")
    real_journal = os.path.join(real_state, "journal.jsonl")
    real_before = (os.path.getsize(real_journal), os.path.getmtime(real_journal)) \
        if real_journal and os.path.exists(real_journal) else None

    # ② 临时账重定向 —— 必须发生在首次 import v2_gate 之前（STATE_DIR 在 import 时定）
    tmp = tempfile.mkdtemp(prefix="v2mcp_")
    os.environ["USERPROFILE"] = tmp
    norm = lambda p: os.path.normcase(os.path.normpath(p))
    try:
        G, B, C = _mods()
        ck("STATE_DIR 重定向到临时目录", norm(G.STATE_DIR).startswith(norm(tmp)), G.STATE_DIR)
        ck("JOURNAL 在临时目录", norm(G.JOURNAL).startswith(norm(tmp)), G.JOURNAL)

        # ③ 进程内两值回环：记 1 -> 借回 1；记 0 -> 借回 0；没记过 -> no_record
        c1 = "MCP自测-记一条-临时账勿当真"
        ctx1 = C.make("mcp-selftest", c1)
        ok1 = G.record("mcp-selftest", c1, 1, "selftest", ctx_fp=ctx1)
        b1 = B.borrow(G._key("mcp-selftest", c1), ctx_fp=ctx1, msg=c1)
        ck("record(verdict=1) ok", ok1)
        ck("borrow 回环 verdict==1",
           bool(b1.get("borrowed")) and (b1.get("node") or {}).get("verdict") == 1,
           b1.get("why_not") or b1.get("error"))

        c2 = "MCP自测-记零-临时账"
        ctx2 = C.make("mcp-selftest", c2)
        ok2 = G.record("mcp-selftest", c2, 0, "selftest", ctx_fp=ctx2)
        b2 = B.borrow(G._key("mcp-selftest", c2), ctx_fp=ctx2, msg=c2)
        ck("borrow 回环 verdict==0",
           ok2 and bool(b2.get("borrowed")) and (b2.get("node") or {}).get("verdict") == 0,
           b2.get("why_not") or b2.get("error"))

        b3 = B.borrow("deadbeefdeadbeef", msg="压根没记过的一句话")
        ck("没记过 -> why_not=no_record",
           (not b3.get("borrowed")) and b3.get("why_not") == "no_record", b3.get("why_not"))

        # ④ stdio 端到端：拉起真 server 子进程，走完整 MCP 握手 + 两工具各一发
        env = dict(os.environ)
        env["USERPROFILE"] = tmp
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", bufsize=1)
        q, errq = queue.Queue(), queue.Queue()
        threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()
        threading.Thread(target=lambda: [errq.put(l) for l in p.stderr], daemon=True).start()

        def send(o):
            p.stdin.write(json.dumps(o, ensure_ascii=False) + "\n")
            p.stdin.flush()

        def recv(timeout=30):
            try:
                return json.loads(q.get(timeout=timeout))
            except queue.Empty:
                tail = ""
                while not errq.empty():
                    tail += errq.get_nowait()
                raise RuntimeError(f"等响应超时; stderr={tail[:500]}")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "v2-selftest", "version": "0"}}})
            r = recv()
            pv = (r.get("result") or {}).get("protocolVersion")
            ck("stdio initialize 握手", r.get("id") == 1 and pv in HANDSHAKE_PROTOCOLS, pv)

            send({"jsonrpc": "2.0", "method": "notifications/initialized"})   # 通知不许回包
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            r = recv()
            tools = (r.get("result") or {}).get("tools") or []
            names = [t.get("name") for t in tools]
            ck("tools/list = v2_record + v2_borrow", names == ["v2_record", "v2_borrow"], names)
            desc_all = " ".join(t.get("description", "") for t in tools)
            ck("描述写清 0/1/None 语义",
               "0" in desc_all and "1" in desc_all and ("null" in desc_all or "None" in desc_all))

            c3 = "MCP自测-stdio端到端-临时账"
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "v2_record",
                             "arguments": {"content": c3, "verdict": 1,
                                           "reason": "selftest", "platform": "mcp-selftest"}}})
            r = recv()
            res = r.get("result") or {}
            pay = json.loads(res["content"][0]["text"])
            ck("stdio v2_record 落账 ok",
               res.get("isError") is False and pay.get("ok") is True and pay.get("verdict") == 1, pay)

            send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "v2_borrow",
                             "arguments": {"content": c3, "platform": "mcp-selftest"}}})
            r = recv()
            res = r.get("result") or {}
            pay = json.loads(res["content"][0]["text"])
            ck("stdio v2_borrow 借回同 verdict==1",
               res.get("isError") is False and pay.get("borrowed") is True
               and pay.get("verdict") == 1 and pay.get("why_not") is None, pay)

            send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "v2_record",
                             "arguments": {"content": "x", "verdict": None,
                                           "platform": "mcp-selftest"}}})
            r = recv()
            ck("verdict=null -> isError（不入账）",
               (r.get("result") or {}).get("isError") is True, r.get("result"))

            send({"jsonrpc": "2.0", "id": 6, "method": "no/such", "params": {}})
            r = recv()
            ck("未知方法 -> -32601", (r.get("error") or {}).get("code") == -32601, r)
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
                p.wait(timeout=5)
        ck("server 收 EOF 干净退出", p.returncode == 0, p.returncode)

        # ⑤ 配置样例能解析
        ex = os.path.join(HERE, "v2_mcp_config_example.json")
        try:
            cfg = json.load(open(ex, encoding="utf-8"))
            ck("配置样例含 mcp_servers.v2",
               isinstance((cfg.get("mcp_servers") or {}).get("v2"), dict))
        except Exception as e:
            ck("配置样例含 mcp_servers.v2", False, e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 真账软观察（只报不判：调用方可能在自测期间正常写账）
    real_after = (os.path.getsize(real_journal), os.path.getmtime(real_journal)) \
        if real_journal and os.path.exists(real_journal) else None
    print(f"INFO 真账 journal 前后（软观察，不判定）: {real_before} -> {real_after}", flush=True)

    all_ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": all_ok, "passed": sum(1 for c in checks if c["ok"]),
                      "total": len(checks), "checks": checks},
                     ensure_ascii=False, indent=2), flush=True)
    return 0 if all_ok else 1


def main():
    ap = argparse.ArgumentParser(description="V2 标准件 MCP 工具档 server（stdio）")
    ap.add_argument("--selftest", action="store_true", help="临时账端到端自测，不碰真账")
    ap.add_argument("--version", action="store_true", help="打印版本号")
    args = ap.parse_args()

    if args.version:
        print(f"v2_mcp_server {SERVER_VERSION}")
        try:
            import v2_gate
            print(f"v2_gate {getattr(v2_gate, '__version__', '?')}")
        except Exception as e:
            print(f"v2_gate import failed: {e}")
        return 0
    if args.selftest:
        return selftest()
    try:
        serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
