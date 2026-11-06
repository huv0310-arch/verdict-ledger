# install.ps1 —— V2 标准件安装器（2026-11-05）
# 拷五件到目标目录 + py_compile 自检 + 打印接线提示。
# 2026-11-05 补漏装的 v2_gate/ledger.py——v2_gate 靠它记账，漏装=record 静默不写（实证）。
# 用法:
#   powershell -File install.ps1 -Target <目标scripts目录>
#   powershell -File install.ps1                 # 不给目标 = 只做自检，不拷贝
param(
    [string]$Target = "",
    [string]$Src = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # 中文读数不乱码
$parts = @("v2_gate.py", "v2_borrow.py", "v2_mcp_server.py", "ctx_fp.py", "v2_gate/ledger.py")

Write-Host "== V2 标准件安装器 v2.0 (src=$Src) =="

# 1) 源件齐不齐 + 版本号读数
foreach ($f in $parts) {
    $p = Join-Path $Src $f
    if (-not (Test-Path $p)) { Write-Host "[FAIL] 缺源件: $p"; exit 1 }
}
$gateVer = (Select-String -Path (Join-Path $Src "v2_gate.py") -Pattern '^__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$borVer  = (Select-String -Path (Join-Path $Src "v2_borrow.py") -Pattern '^__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "源件: v2_gate=$gateVer  v2_borrow=$borVer"

# 2) 拷贝到目标目录（v2_gate/ledger.py 要先建子目录，Copy-Item 不会自动建）
if ($Target -ne "") {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    foreach ($f in $parts) {
        $dst = Join-Path $Target $f
        $dstDir = Split-Path $dst -Parent
        if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
        Copy-Item -Path (Join-Path $Src $f) -Destination $dst -Force
    }
    Write-Host "已拷五件(含 v2_gate/ledger.py) -> $Target"
}

# 3) py_compile 自检（对目标目录里的副本编译；无目标就编源件）
$dir = if ($Target -ne "") { $Target } else { $Src }
foreach ($f in $parts) {
    $p = Join-Path $dir $f
    python -m py_compile $p
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] py_compile 不过: $p"; exit 1 }
    Write-Host "py_compile OK: $f"
}

# 4) 接线提示（装完照这个接，判据一字不动）
Write-Host @"

---- 接线提示 ----
1. 接线: 在你的入口 import v2_borrow（只读借账），或直接起 v2_mcp_server.py 挂 MCP 工具表。
2. key/ctx_fp 公式取自 ctx_fp.py —— 两端必须同一份文件，否则同一件事算不出同一个 key。
3. 状态文件（账本/锁/归档）都认环境变量 V2_STATE_DIR，默认 %USERPROFILE%\.hermes\state\v2。
4. 账本格式（journal.jsonl + ledger.json + 归档 expired.jsonl）见 README.md「边界」节。
5. 验收: python -m py_compile 全过 + python v2_borrow.py demo（三路）+ python v2_borrow.py（自检）。
6. 装完 sha256 必须与源一致（拷贝不许有改动）。
"@
Write-Host "== 完成 =="
