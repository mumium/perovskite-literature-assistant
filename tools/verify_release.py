"""Run the single exe from an isolated folder without external Python/Office PATH."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

workspace = Path(__file__).resolve().parents[1]
executable = workspace / "dist" / "钙钛矿文献助手.exe"
root = workspace / "test-output" / ("独立EXE验收 " + time.strftime("%Y%m%d-%H%M%S"))
root.mkdir(parents=True)
binary_folder = root / "只放一个exe"
binary_folder.mkdir()
portable = binary_folder / executable.name
shutil.copyfile(executable, portable)
temporary = root / "临时 解包"
temporary.mkdir()
env = dict(os.environ)
env["PATH"] = str(Path(env.get("SYSTEMROOT") or env.get("SystemRoot") or "C:/Windows") / "System32")
for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"]:
    env.pop(name, None)
env["TEMP"] = env["TMP"] = str(temporary)
env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
output = root / "离线 中文 空格 验收"
started = time.monotonic()
run = subprocess.run([str(portable), "--smoke-test", str(output)], cwd=binary_folder, env=env, timeout=180, capture_output=True)
time.sleep(1)
report = {"exe": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "size_bytes": executable.stat().st_size,
          "exit_code": run.returncode, "elapsed_seconds": round(time.monotonic()-started, 2),
          "only_exe_in_launch_folder": len(list(binary_folder.iterdir()))==1,
          "external_runtime_path_removed": True, "network_connections_blocked_in_smoke_test": True,
          "temporary_MEI_remaining": [p.name for p in temporary.glob("_MEI*")], "output": str(output),
          "clean_windows_vm_tested": False}
for name in ["selftest-report.json", "gui-report.json"]:
    if (output / name).exists():
        report[name] = json.loads((output / name).read_text(encoding="utf-8"))
if run.stderr:
    report["stderr"] = run.stderr.decode("utf-8", errors="replace")
gui=report.get("gui-report.json",{})
passed = run.returncode==0 and report.get("selftest-report.json",{}).get("passed") and all(gui.get(k) for k in ["pdf_preview","original_hash_unchanged","pdf_highlight_persisted","zoom_persisted","docx_highlight_persisted","native_pdf_annotations","write_back_backup","automatic_original","compact_layout"]) and report["only_exe_in_launch_folder"] and not report["temporary_MEI_remaining"]
report["passed"] = bool(passed)
(workspace / "dist" / "验收报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
# Developer consoles may use cp1252 even when paths contain Chinese characters.
print(json.dumps({"passed":passed,"exit_code":run.returncode,"output":str(output)}, ensure_ascii=True))
sys.exit(0 if passed else 1)
