# 第三方依赖

运行包使用以下项目，具体版本以 requirements.txt 和构建依赖锁为准。该文件不替代各依赖完整许可；构建时的 `dist/licenses/` 收集发行包许可文本和元数据。

| 项目 | 许可参考 |
|---|---|
| PySide6 / shiboken6 / Qt | LGPLv3/GPLv3/商业许可，https://www.qt.io/licensing |
| PyMuPDF / MuPDF | AGPLv3/商业许可，https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright |
| python-docx | MIT |
| openpyxl | MIT |
| lxml | BSD，与底层libxml2/libxslt许可并存 |
| jsonschema、attrs、referencing、rpds-py | MIT等，各发行包元数据与许可文件为准 |
| PyInstaller | GPL，附允许分发构建产物的例外，https://pyinstaller.org/en/stable/license.html |
| Python | PSF许可 |

源代码已一并交付。对外分发本程序时应遵守所选第三方许可及其源代码、修改和重新链接等要求，或获取商业许可。
