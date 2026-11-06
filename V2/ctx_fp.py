# -*- coding: utf-8 -*-
"""ctx_fp.py —— 情形指纹公式（2026-11-04 v2）

两套指纹，口径分家：
  * ctx_fp（本文件，主账栏）  = **只看这节账自己**：平台 + 归一化问法。
    各端拿同一份代码算，同一件事必然同指纹 → 能跨端对账。
  * spiral_fp（spiral_pi 另存账）= 看**它在整条账本里的位次**（θ|r|z 螺旋坐标）。
    依赖账本长度和排序，各端账不一样长 → 同一件事会算出三个值，
    **只能自家取材用（走到哪取哪），绝不进对账**。

v2 改动（相对 01:20 那版 platform::msg[:64]）：
  1. 加域分隔 "ctx2::" → 指纹永远不等于地址 key（短句不再退化成 key 的复制品）
  2. 归一化：去标点/空白/数字/emoji → 「现在去跑？」和「现在去跑」= 同一情形
  3. 带版本号 → 以后公式再改，读账的一眼分得出是哪版算的

跨端可比的前提：**各端都得用这一份文件**（发出去的就是它）。
"""
import hashlib
import re

VERSION = "ctx2"
_KEEP = re.compile(r"[\W_0-9]+", re.UNICODE)   # 去标点/空白/数字/下划线，只留中英文
MAX_NORM = 64                                    # 归一化后取前 64 字（问法略变仍算同一情形）


def normalize(msg):
    return _KEEP.sub("", str(msg or ""))[:MAX_NORM]


def make(platform, msg):
    """情形指纹 16 位 hex。纯函数：同输入必同输出，各端无状态可复算。"""
    raw = "%s::%s::%s" % (VERSION, platform, normalize(msg))
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


# ---------- 各端同款地址 key（设计决定 2026-11-04：key 只认内容不认平台） ----------
# 旧 key = v2_gate._key: sha256("{platform}::{msg}")[:16] —— 带平台、原文不清洗 → 各端各算各的、交集 0。
# 新 key 三条：不看表头 + key 是公转 → 不带平台；同维度才握手 → 清洗与 ctx_fp 同一份；
# 不含 ts → 79.3s 钟差进不来。旧账标「世代 v1」不回改（旧圈不删）。
KEY_VER = "k3"


def key_of(msg):
    """各端同款地址 key：只哈希清洗后的内容。纯函数、无状态、可复算。"""
    raw = "%s::%s" % (KEY_VER, normalize(msg))
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


if __name__ == "__main__":
    a = make("chat", "现在去跑？")
    b = make("chat", "现在去跑")
    k = key_of("现在去跑？")
    print("ctx(带标点) =", a)
    print("ctx(不带标点) =", b, "→ 同一情形:", a == b)
    print("地址 key =", k, "→ 与指纹分家:", a != k)


# ---------- 公共坐标（v2.3，2026-11-04） ----------
# 与 ctx_fp 分家：ctx_fp 只看这节账自己（跨端对账），本函数给的是**位置**（第几号轨道）。
# n 由各端共享的 key 派生 → 各端同值；不含 ts，所以 79.3s 钟差进不了公式。
# 「同 key 一条轨道」的落地件：同 key 必落同一条轨道（公转），账长无关。
GOLDEN_ANGLE_DEG = 137.507764050037854
CROSS_MOD = 10000000


def cross(key):
    """公共坐标指纹 16 位 hex。纯函数：同 key 必同值，各端无状态可复算。"""
    n = int(str(key)[:8], 16) % CROSS_MOD
    theta = (n * GOLDEN_ANGLE_DEG) % 360.0
    raw = "cross2::%d|%.3f|%.3f" % (n, theta, n ** 0.5)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def cross_pos(key):
    """同上的位置（人能读）：θ / r / 轨道号（8 条公共轨道）。"""
    n = int(str(key)[:8], 16) % CROSS_MOD
    theta = (n * GOLDEN_ANGLE_DEG) % 360.0
    return {"n": n, "theta_deg": round(theta, 3), "r": round(n ** 0.5, 3),
            "bucket": int(theta // 45.0)}
