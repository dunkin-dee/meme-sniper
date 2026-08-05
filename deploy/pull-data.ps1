<#
.SYNOPSIS
    Pull a consistent snapshot of the recorder database from the EC2 host.

.DESCRIPTION
    A WAL-mode SQLite database must never be copied with scp/rsync while it is
    being written - you get a torn file with a detached WAL. This runs
    `.backup` on the remote first, which takes an atomic, transactionally
    consistent snapshot, then transfers that.

    Keeps timestamped copies so a corrupted pull can never overwrite the last
    good one.

.EXAMPLE
    .\deploy\pull-data.ps1 -RemoteHost ec2-1-2-3-4.compute.amazonaws.com -KeyFile ~\.ssh\sniper.pem
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RemoteHost,
    [string]$User = "ubuntu",
    [string]$KeyFile,
    [string]$RemoteDb = "/opt/meme-sniper/data/sniper.db",
    [string]$LocalDir = "data/pulls",
    [int]$KeepLast = 10
)

$ErrorActionPreference = "Stop"

# `.backup` on a multi-GB database is slow and silent; without keepalives an
# idle-timed-out channel leaves this hanging on a half-open socket.
$sshArgs = @("-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6")
if ($KeyFile) { $sshArgs += @("-i", $KeyFile) }
$target = "$User@$RemoteHost"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$remoteSnap = "/tmp/sniper-snapshot-$stamp.db"

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

Write-Host "==> Taking consistent snapshot on $RemoteHost" -ForegroundColor Cyan
# sudo -u sniper so the snapshot is readable and respects file ownership.
$backupCmd = "sudo -u sniper sqlite3 '$RemoteDb' `".backup '$remoteSnap'`" && sudo chmod 644 '$remoteSnap' && ls -l '$remoteSnap'"
& ssh @sshArgs $target $backupCmd
if ($LASTEXITCODE -ne 0) { throw "remote snapshot failed (exit $LASTEXITCODE)" }

$localPath = Join-Path $LocalDir "sniper-$stamp.db"
Write-Host "==> Transferring to $localPath" -ForegroundColor Cyan
& scp @sshArgs "${target}:$remoteSnap" $localPath
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE)" }

& ssh @sshArgs $target "rm -f '$remoteSnap'" | Out-Null

Write-Host "==> Verifying integrity" -ForegroundColor Cyan
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "py -3.11" }
$check = & cmd /c "$py -c ""import sqlite3,sys; c=sqlite3.connect(r'$localPath'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); print(c.execute('SELECT COUNT(*) FROM launches').fetchone()[0])"" 2>&1"
$lines = @($check)
if ($lines[0] -ne "ok") {
    Write-Host "INTEGRITY CHECK FAILED - keeping file for inspection, not promoting" -ForegroundColor Red
    $lines | ForEach-Object { Write-Host "   $_" }
    exit 1
}
Write-Host "    integrity: ok" -ForegroundColor Green
Write-Host "    launches:  $($lines[1])" -ForegroundColor Green

# Promote to the stable path the analysis notebook reads.
$latest = Join-Path $LocalDir "sniper-latest.db"
Copy-Item $localPath $latest -Force
Write-Host "==> Promoted to $latest" -ForegroundColor Green

# Prune old pulls, never the promoted copy.
$old = Get-ChildItem $LocalDir -Filter "sniper-2*.db" |
       Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepLast
if ($old) {
    $old | Remove-Item -Force
    Write-Host "    pruned $($old.Count) old snapshot(s)" -ForegroundColor DarkGray
}
