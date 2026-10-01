# Smoke test

For the Codex footer installer, run from the repository root.

## Floor

```powershell
python scripts/verify_codex_install.py
```

## Bar

Run the real installer with an isolated home. A custom footer must remain
byte-identical and the installer must report `already current`.

```powershell
$smokeHome = Join-Path $env:TEMP ('codex-footer-' + [guid]::NewGuid())
$previousProfile = $env:USERPROFILE
try {
    $env:USERPROFILE = $smokeHome
    New-Item -ItemType Directory -Path "$smokeHome/.codex" | Out-Null
    $configuration = "$smokeHome/.codex/config.toml"
    $customFooter = "[tui]`nstatus_line = ['context-remaining']`nstatus_line_use_colors = false`n"
    [System.IO.File]::WriteAllText($configuration, $customFooter)
    python install.py --repo . --platform codex
    if ($LASTEXITCODE -ne 0) { throw 'Installer failed' }
    if ([System.IO.File]::ReadAllText($configuration) -cne $customFooter) {
        throw 'Custom footer changed'
    }
} finally {
    $env:USERPROFILE = $previousProfile
}
```

The temporary home may be kept for inspection. No running Codex restart is
needed to verify installer preservation.
