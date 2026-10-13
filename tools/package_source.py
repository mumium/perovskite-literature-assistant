"""Create the source delivery after the final build and acceptance test."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
report = json.loads((dist / "验收报告.json").read_text(encoding="utf-8"))
exe = dist / "钙钛矿文献助手.exe"
sha = hashlib.sha256(exe.read_bytes()).hexdigest()
if not report.get("passed") or report["sha256"] != sha:
    raise RuntimeError("请先对当前exe运行verify_release.py并通过验收")
(dist / "SHA256.txt").write_text(sha + "  钙钛矿文献助手.exe\n", encoding="utf-8")
shutil.copyfile(root / "README.md", dist / "使用说明.md")
shutil.copyfile(Path(report["output"]) / "GUI故事.png", dist / "界面预览.png")
shutil.copyfile(Path(report["output"]) / "GUI原文.png", dist / "PDF预览.png")
include = [root / n for n in ["app.py", "README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt", "requirements-lock.txt", "pyproject.toml", "build_windows.ps1", "钙钛矿文献助手.spec", ".gitignore"]]
for directory in ["perovskite", "tests", "tools", "docs", "assets", ".github", "dist/licenses"]:
    include.extend(p for p in (root / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
include += [dist / "dependency-versions.json"]
public_report={k:report[k] for k in ["sha256","size_bytes","exit_code","elapsed_seconds","only_exe_in_launch_folder","external_runtime_path_removed","network_connections_blocked_in_smoke_test","temporary_MEI_remaining","clean_windows_vm_tested","passed"]}
public_report["gui_checks"]={k:v for k,v in report.get("gui-report.json",{}).items() if isinstance(v,bool)}
public_report["synthetic_only"]=True
(dist / "公开验收报告.json").write_text(json.dumps(public_report,ensure_ascii=False,indent=2),encoding="utf-8")
target = dist / "钙钛矿文献助手-源码与说明.zip"
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for p in sorted(set(include)):
        archive.write(p, p.relative_to(root).as_posix())
    archive.writestr("dist/验收报告.json",json.dumps(public_report,ensure_ascii=False,indent=2))
print("Source package:", target.stat().st_size, "bytes; exe:", exe.stat().st_size, "bytes")
