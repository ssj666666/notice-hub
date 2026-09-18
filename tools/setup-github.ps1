# ============================================================
#  GitHub connectivity diagnostic and proxy setup
#
#  Usage (run AFTER you turn on your VPN/proxy):
#      powershell -ExecutionPolicy Bypass -File tools\setup-github.ps1
#  Add -Apply to actually write git/gh proxy settings:
#      powershell -ExecutionPolicy Bypass -File tools\setup-github.ps1 -Apply
#
#  NOTE: kept ASCII-only on purpose. Windows PowerShell reads BOM-less
#        UTF-8 scripts as ANSI, which mangles non-ASCII strings and
#        breaks parsing.
# ============================================================
param([switch]$Apply)

$ErrorActionPreference = 'Continue'

function Test-Github {
    param([int]$TimeoutSec = 12, [string]$Via = '')
    try {
        $args = @{ Uri = 'https://github.com'; UseBasicParsing = $true; TimeoutSec = $TimeoutSec }
        if ($Via) { $args['Proxy'] = $Via }
        $r = Invoke-WebRequest @args
        return $r.StatusCode
    } catch { return $null }
}

Write-Host '=== 1. Looking for a proxy ===' -ForegroundColor Cyan

$proxy = $null

$reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
if ($reg.ProxyEnable -eq 1 -and $reg.ProxyServer) {
    $proxy = $reg.ProxyServer
    if ($proxy -notmatch '^\w+://') { $proxy = "http://$proxy" }
    Write-Host "  System proxy: $proxy" -ForegroundColor Green
}

if (-not $proxy) {
    foreach ($n in 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy') {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ($v) { $proxy = $v; Write-Host "  Env $n = $v" -ForegroundColor Green; break }
    }
}

if (-not $proxy) {
    Write-Host '  No system proxy / env var. Scanning common local proxy ports...'
    $ports = 7890, 7891, 7897, 7899, 10808, 10809, 1080, 1081, 8080, 8888, 20171, 33210, 2080, 20172
    $listening = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $ports } |
        Select-Object -ExpandProperty LocalPort -Unique
    if (-not $listening) {
        Write-Host "  Nothing listening on: $($ports -join ', ')" -ForegroundColor Yellow
    }
    foreach ($p in $listening) {
        $candidate = "http://127.0.0.1:$p"
        $code = Test-Github -Via $candidate -TimeoutSec 10
        if ($code -eq 200) {
            $proxy = $candidate
            Write-Host "  FOUND: port $p works as an HTTP proxy (GitHub returned 200)" -ForegroundColor Green
            break
        } else {
            Write-Host "  port $p is listening but does not proxy GitHub"
        }
    }
}

Write-Host ''
Write-Host '=== 2. Direct connection to github.com (3 tries) ===' -ForegroundColor Cyan
$ok = 0
for ($i = 1; $i -le 3; $i++) {
    $code = Test-Github
    if ($code) { $ok++; Write-Host "  try $i : HTTP $code" -ForegroundColor Green }
    else       { Write-Host "  try $i : FAILED" -ForegroundColor Red }
}
Write-Host "  Direct success rate: $ok/3"

Write-Host ''
Write-Host '=== 3. Through the proxy (if any) ===' -ForegroundColor Cyan
if ($proxy) {
    $ok2 = 0
    for ($i = 1; $i -le 3; $i++) {
        if ((Test-Github -Via $proxy) -eq 200) { $ok2++ }
    }
    Write-Host "  Via $proxy success rate: $ok2/3"

    if ($ok2 -eq 0) {
        Write-Host '  Proxy does not reach GitHub. Nothing written.' -ForegroundColor Yellow
    } elseif ($Apply) {
        Write-Host ''
        Write-Host '=== 4. Writing config (-Apply) ===' -ForegroundColor Cyan
        git config --global http.proxy $proxy
        git config --global https.proxy $proxy
        Write-Host "  git http.proxy  = $proxy"
        Write-Host "  git https.proxy = $proxy"
        $env:HTTPS_PROXY = $proxy
        $env:HTTP_PROXY = $proxy
        Write-Host '  HTTPS_PROXY set for this session (gh reads it)'
        Write-Host ''
        Write-Host 'Now run:  gh auth login' -ForegroundColor Yellow
    } else {
        Write-Host '  Proxy works. Re-run with -Apply to write git/gh config.' -ForegroundColor Yellow
    }
} else {
    Write-Host '  No usable proxy found.' -ForegroundColor Yellow
    Write-Host '  If your VPN runs in "global mode", direct success rate above should become 3/3.'
}

Write-Host ''
Write-Host '=== 5. SSH channel (does not need a proxy) ===' -ForegroundColor Cyan
$sshOut = (ssh -T -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12 git@github.com 2>&1 | Out-String)
if ($sshOut -match 'successfully authenticated') {
    Write-Host '  OK: SSH authenticated' -ForegroundColor Green
} elseif ($sshOut -match 'Permission denied') {
    Write-Host '  SSH reachable, but the public key is not added to GitHub yet' -ForegroundColor Yellow
} else {
    Write-Host "  SSH output: $($sshOut.Trim().Split([char]10)[0])" -ForegroundColor Yellow
}
Write-Host "  Public key file: $env:USERPROFILE\.ssh\id_ed25519.pub"
