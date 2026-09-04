<#
.SYNOPSIS
    Syncs C:\Dev\redactor_common from a redactor_common_*.zip / *_with_redactor_common.zip
    drop, commits, and pushes to origin/main. Fully automated: no prompts.

.DESCRIPTION
    1. Picks the newest matching zip in -SourceDir (or use -ZipPath to name one explicitly).
    2. Extracts it to a scratch folder.
    3. Finds the redactor_common/ source tree inside it. A drop can contain more than one
       candidate (e.g. a stale root-level redactor_common/ alongside the real
       <project>/redactor_common/ in some "*_with_redactor_common.zip" bundles) - whichever
       candidate has the most files under core/+gui/+tests/ wins, since the stale copy is
       always a subset of the current one.
    4. Syncs ONLY the items that exist at the top of that source tree (core/, gui/, tests/,
       README.md, .gitignore, __init__.py, ...) onto the repo working copy - each source
       subfolder is mirrored (so file deletions upstream are reflected), each source file is
       copied. Anything in the repo that ISN'T part of the source tree (.git/, tools/, any
       future repo-only file) is never touched - deliberately NOT a whole-root mirror, so
       this script can't delete itself or other repo-only content.
    5. If anything changed: git add -A, commit, push origin main.
    6. If nothing changed: does nothing (no empty commit).

.PARAMETER ZipPath
    Explicit zip to sync from. If omitted, the newest matching zip in -SourceDir is used.

.PARAMETER SourceDir
    Folder to look for drop zips in when -ZipPath isn't given. Default: C:\Temp\_AI Coding

.PARAMETER RepoPath
    The working copy to sync into. Default: C:\Dev\redactor_common

.PARAMETER NoPush
    Commit locally but skip the push (for a dry run / manual review before pushing).

.EXAMPLE
    powershell -File tools\sync-from-zip.ps1
    # Finds the newest redactor_common*.zip / *_with_redactor_common.zip drop, syncs, commits, pushes.

.EXAMPLE
    powershell -File tools\sync-from-zip.ps1 -ZipPath "C:\Temp\_AI Coding\mp3redactor_with_redactor_common.zip"
#>
[CmdletBinding()]
param(
    [string]$ZipPath,
    [string]$SourceDir = "C:\Temp\_AI Coding",
    [string]$RepoPath  = "C:\Dev\redactor_common",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

function Get-RedactorCommonCandidates([string]$ExtractedRoot) {
    # Case A: zip root IS redactor_common\ itself
    $candidates = @()
    $direct = Join-Path $ExtractedRoot "redactor_common"
    if (Test-Path (Join-Path $direct "core")) { $candidates += $direct }

    # Case B: zip contains <project>\redactor_common\  (a "*_with_redactor_common.zip" bundle)
    Get-ChildItem $ExtractedRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $nested = Join-Path $_.FullName "redactor_common"
        if (Test-Path (Join-Path $nested "core")) { $candidates += $nested }
    }
    return $candidates
}

function Get-CandidateFileCount([string]$Path) {
    (Get-ChildItem $Path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
}

# 1. Resolve the zip to sync from
if (-not $ZipPath) {
    $found = Get-ChildItem $SourceDir -Filter "*.zip" -ErrorAction Stop |
        Where-Object { $_.Name -match "redactor_common" } |
        Sort-Object LastWriteTime -Descending
    if (-not $found) {
        throw "No redactor_common*.zip / *_with_redactor_common.zip found in '$SourceDir'."
    }
    $ZipPath = $found[0].FullName
}
if (-not (Test-Path $ZipPath)) { throw "Zip not found: $ZipPath" }
Write-Host "Using zip: $ZipPath"

if (-not (Test-Path $RepoPath)) { throw "Repo not found at $RepoPath. Clone/set it up first." }

# 2. Extract to a scratch folder
$work = Join-Path $env:TEMP ("redactor_common_sync_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Expand-Archive -Path $ZipPath -DestinationPath $work -Force

    # 3. Locate the source tree - pick the candidate with the most files, so a stale
    #    leftover copy (subset of the real one) never wins over the current one.
    $candidates = Get-RedactorCommonCandidates $work
    if (-not $candidates) {
        throw "Could not find a redactor_common/core folder inside $ZipPath"
    }
    $srcTree = $candidates |
        Sort-Object { Get-CandidateFileCount $_ } -Descending |
        Select-Object -First 1
    if ($candidates.Count -gt 1) {
        Write-Host "Multiple redactor_common/ candidates found in the zip; using the most complete one:"
        $candidates | ForEach-Object { Write-Host "  $_  ($(Get-CandidateFileCount $_) files)" }
    }
    Write-Host "Source tree: $srcTree"

    # 4. Sync ONLY what the source tree actually contains - never a whole-root mirror,
    #    so repo-only content (.git, tools/, ...) is left alone.
    Get-ChildItem $srcTree -Force | ForEach-Object {
        $destPath = Join-Path $RepoPath $_.Name
        if ($_.PSIsContainer) {
            $null = robocopy $_.FullName $destPath /MIR /NFL /NDL /NJH /NJS
            if ($LASTEXITCODE -ge 8) {
                throw "robocopy failed (exit $LASTEXITCODE) mirroring $($_.FullName) -> $destPath"
            }
        } else {
            Copy-Item -Path $_.FullName -Destination $destPath -Force
        }
    }

    # 5. Commit + push if anything changed
    Push-Location $RepoPath
    try {
        git add -A
        $staged = git diff --cached --name-only
        if (-not $staged) {
            Write-Host "No changes vs. current repo state - nothing to commit."
            return
        }

        $stamp     = Get-Date -Format "yyyy-MM-dd HH:mm"
        $zipName   = Split-Path $ZipPath -Leaf
        $fileCount = ($staged | Measure-Object).Count
        $msg = "redactor_common auto-sync: $zipName ($stamp, $fileCount file(s) changed)`n`nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"

        git commit -q -m $msg
        Write-Host "Committed: $zipName ($fileCount file(s) changed)"

        if ($NoPush) {
            Write-Host "NoPush set - leaving commit local (not pushed)."
        } else {
            git push origin main
            Write-Host "Pushed to origin/main."
        }
    } finally {
        Pop-Location
    }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
