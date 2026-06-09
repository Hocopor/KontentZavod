# Stop hook: blocks finishing the turn if project files changed after the last STATE.md update.
# ASCII only! PowerShell 5.1 reads BOM-less ps1 as ANSI and breaks on Cyrillic.
$inputJson = [Console]::In.ReadToEnd()
try { $payload = $inputJson | ConvertFrom-Json } catch { $payload = $null }
# loop guard: if we already blocked this Stop, let it pass
if ($payload -and $payload.stop_hook_active) { exit 0 }

$root = "A:\DevAI\Projects\KontentZavod"
$state = Get-Item (Join-Path $root "STATE.md") -ErrorAction SilentlyContinue
if (-not $state) { exit 0 }

$dirs = @($root)
$router = "A:\DevAI\Projects\LLMRouter"
if (Test-Path $router) { $dirs += $router }

$exclude = '\\(\.claude|\.git|data|__pycache__|\.venv|node_modules)(\\|$)'
$newest = $null
foreach ($d in $dirs) {
    $files = Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch $exclude -and $_.Name -ne 'STATE.md' -and $_.Name -ne 'PLAN.md' }
    foreach ($f in $files) {
        if ($null -eq $newest -or $f.LastWriteTime -gt $newest) { $newest = $f.LastWriteTime }
    }
}

if ($newest -and $newest -gt $state.LastWriteTime) {
    $msg = "Project files changed after the last STATE.md update. Before finishing: (1) update STATE.md in A:\DevAI\Projects\KontentZavod - current point, what was done and the result, next step; (2) tick completed checkboxes in PLAN.md; (3) add any new gotchas to STATE.md -> Nuances section."
    @{ decision = "block"; reason = $msg } | ConvertTo-Json -Compress
}
exit 0
