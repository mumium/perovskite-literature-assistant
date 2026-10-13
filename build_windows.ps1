param([string]$Python = "python", [switch]$SkipInstall)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath ".venv/Scripts/python.exe")) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "创建构建虚拟环境失败" }
}
$BuildPython = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
if (-not $SkipInstall) {
    & $BuildPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败" }
}
& $BuildPython -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "测试未通过，停止打包" }
& $BuildPython tools/collect_licenses.py dist/licenses
if ($LASTEXITCODE -ne 0) { throw "收集依赖许可失败" }
& $BuildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name "钙钛矿文献助手" --icon "assets/logo-book-octahedron.ico" --noupx --add-data "assets;assets" --add-data "README.md;help" --add-data "docs;help/docs" --add-data "dist/licenses;licenses" --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller构建失败" }
$BuiltExe = Join-Path $PSScriptRoot "dist/钙钛矿文献助手.exe"
$Digest = (Get-FileHash -LiteralPath $BuiltExe -Algorithm SHA256).Hash
Set-Content -LiteralPath "dist/SHA256.txt" -Value "$Digest  钙钛矿文献助手.exe" -Encoding utf8
Write-Host "已构建：$BuiltExe"
