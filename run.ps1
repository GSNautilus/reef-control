# Starts Reef Control and opens it in the browser. Close the window to stop it.
Set-Location -Path $PSScriptRoot
# Uses the "codex" conda environment if it exists, otherwise python on PATH.
$Python = Join-Path $env:USERPROFILE "miniconda3\envs\codex\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python -m app
