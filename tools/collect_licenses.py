import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path

target = Path(sys.argv[1] if len(sys.argv)>1 else "dist/licenses")
target.mkdir(parents=True, exist_ok=True)
packages = ["PySide6-Essentials", "shiboken6", "PyMuPDF", "python-docx", "openpyxl", "lxml", "jsonschema", "attrs", "jsonschema-specifications", "referencing", "rpds-py", "et-xmlfile", "typing_extensions", "PyInstaller", "pyinstaller-hooks-contrib"]
versions = {}
for name in packages:
    dist = metadata.distribution(name)
    folder = target / name
    folder.mkdir(exist_ok=True)
    versions[name] = dist.version
    (folder / "METADATA.txt").write_text(dist.read_text("METADATA") or json.dumps(dict(dist.metadata), ensure_ascii=False, indent=2), encoding="utf-8")
    for file in dist.files or []:
        if any(s in str(file).lower() for s in ["license", "copying", "copyright", "notice"]) and str(file).lower().endswith((".txt", ".md", "license", "copying", "copyright")):
            source = Path(dist.locate_file(file))
            if source.is_file():
                destination = folder / Path(str(file).replace("..", "_")).name
                if destination.exists():
                    if destination.read_bytes() == source.read_bytes():
                        continue
                    destination = folder / (str(len(list(folder.iterdir()))) + "-" + destination.name)
                shutil.copyfile(source, destination)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copyfile(python_license, target / "Python-LICENSE.txt")
(target.parent / "dependency-versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
