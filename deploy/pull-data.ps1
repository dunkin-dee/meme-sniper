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
#
# Quote with SINGLE quotes only. PowerShell 5.1 mangles embedded double quotes
# when passing an argument to a native executable, so the previous
# `".backup '$remoteSnap'"` arrived at the remote shell with its quoting
# stripped and sqlite3 failed with "missing FILENAME argument on .backup" -
# i.e. the backup path was broken on its first real use. Neither path contains
# spaces, so single quotes are sufficient and survive the trip intact.
$backupCmd = "sudo -u sniper sqlite3 $RemoteDb '.backup $remoteSnap' && sudo chmod 644 $remoteSnap && ls -l $remoteSnap"
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

# integrity_check is NOT sufficient on its own: it returns "ok" for a zero-byte
# file. A failed remote snapshot that produced an empty database would sail
# through and overwrite the last good copy. Demand real rows.
$rows = 0
if (-not [int]::TryParse(($lines[1] -as [string]), [ref]$rows) -or $rows -le 0) {
    Write-Host "PULLED DATABASE HAS NO LAUNCHES - not promoting" -ForegroundColor Red
    $lines | ForEach-Object { Write-Host "   $_" }
    exit 1
}

# launches is append-only, so the count must never shrink. A drop means a torn
# snapshot, a reset database, or the wrong host - never a normal pull.
$latest = Join-Path $LocalDir "sniper-latest.db"
if (Test-Path $latest) {
    $prevOut = & cmd /c "$py -c ""import sqlite3; print(sqlite3.connect(r'$latest').execute('SELECT COUNT(*) FROM launches').fetchone()[0])"" 2>&1"
    $prev = 0
    if ([int]::TryParse((@($prevOut)[0] -as [string]), [ref]$prev) -and $rows -lt $prev) {
        Write-Host "ROW COUNT WENT BACKWARDS: $prev -> $rows - not promoting" -ForegroundColor Red
        Write-Host "   kept at $localPath for inspection" -ForegroundColor Red
        exit 1
    }
}

Write-Host "    integrity: ok" -ForegroundColor Green
Write-Host "    launches:  $rows" -ForegroundColor Green

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
