import hashlib
import re
import threading
from pathlib import Path

DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"]+", re.I)
PDF_LOCK = threading.RLock()
SI = re.compile(r"supporting\s+information|supplement(?:ary|al)|(?:^|[ _.-])si(?:[ _.-]|$)|支持信息|补充", re.I)

def digest(path, cancel=lambda: False):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancel():
                raise InterruptedError("已取消")
            h.update(block)
    return h.hexdigest()

def split_location(base, text, **extra):
    # Preserve every character. Bounded chunks carry stable per-file locations.
    return [{"loc_id": f"{base}:c{i//9000}", "text": text[i:i+9000], **extra} for i in range(0, len(text), 9000)] or [{"loc_id": f"{base}:c0", "text": "", **extra}]

def candidates(locations, name):
    first = "\n".join(x["text"] for x in locations if x.get("page_index") == 0) if any("page_index" in x for x in locations) else "\n".join(x["text"] for x in locations[:12])
    first = re.split(r"(?im)^\s*(?:references\b|bibliography\b|参考文献)(?:[^\n]*)", first)[0]
    # Only explicitly labelled DOI lines in first page/front matter. Bare citation DOIs are ignored.
    found = []
    for line in first.splitlines():
        if re.search(r"(?i)^\s*(?:doi\s*:\s*(?:https?://(?:dx\.)?doi\.org/)?|https?://(?:dx\.)?doi\.org/)", line):
            found.extend(m.group(0).rstrip(".,;)]}") for m in DOI.finditer(line))
    found = list(dict.fromkeys(v.lower() for v in found))
    lines = [s.strip() for s in first.splitlines() if len(s.strip()) > 15 and not DOI.search(s) and not SI.search(s)]
    title = lines[0][:1000] if lines else Path(name).stem
    author_match = re.search(r"(?im)^\s*(?:authors?|作者)\s*:\s*(.+)$", first)
    authors = [s.strip() for s in re.split(r"[,;]", author_match[1]) if s.strip()] if author_match else []
    return {"title": title, "doi": found[0] if len(found) == 1 else None, "doi_candidates": found,
            "authors": authors, "filename": name,
            "role": "si" if SI.search(name + "\n" + first[:400]) else "main",
            "basis": "首页明确DOI标识（仅候选，需核验）" if found else "首页/文件名标题候选，未识别明确DOI"}

def extract(path, cancel=lambda: False):
    path = Path(path)
    locations, warnings = [], []
    suffix = path.suffix.lower()
    try:
        if suffix == ".doc":
            return {"status": "unsupported", "reason": "旧版DOC暂不解析；原件已保存。请在可用电脑另存DOCX后作为新附件导入。", "locations": [], "candidates": candidates([], path.name)}
        if suffix == ".pdf":
            import fitz
            with PDF_LOCK, fitz.open(path) as document:
                if document.needs_pass:
                    raise ValueError("PDF已加密，需要密码；本版不保存密码")
                for index, page in enumerate(document):
                    if cancel():
                        raise InterruptedError("已取消")
                    content = page.get_text("text", sort=True)
                    if len(content.strip()) < 15:
                        warnings.append(f"第{index+1}实际页文字不足，可能扫描/图片页；未进行OCR")
                    locations.extend(split_location(f"pdf:p{index}", content, page_index=index, printed_page=None, kind="page"))
                if not document.page_count:
                    raise ValueError("PDF没有页面")
            warnings.append("图片及复杂表格未作结构识别，请查看PDF原页核验")
            usable = any(len(x["text"].strip()) >= 15 for x in locations)
            status = "partial" if usable and len(warnings) > 1 else "ready" if usable else "no_text"
        elif suffix == ".docx":
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            doc = Document(path)
            heading, p, t = "", 0, 0
            for child in doc.element.body.iterchildren():
                if cancel():
                    raise InterruptedError("已取消")
                if child.tag.endswith("}p"):
                    paragraph = Paragraph(child, doc)
                    if paragraph.style and paragraph.style.name.startswith("Heading"):
                        heading = paragraph.text
                    locations.extend(split_location(f"docx:p{p}", paragraph.text, kind="paragraph", section=heading, paragraph_index=p))
                    p += 1
                elif child.tag.endswith("}tbl"):
                    table = Table(child, doc)
                    for row_i, row in enumerate(table.rows):
                        locations.extend(split_location(f"docx:t{t}:r{row_i}", "\t".join(cell.text for cell in row.cells), kind="table_row", section=heading, table_index=t, row_index=row_i))
                    t += 1
            status = "ready" if any(x["text"].strip() for x in locations) else "no_text"
            warnings.append("DOCX无稳定页码；提取正文段落与表格，文本框/嵌入对象/图片/脚注需人工查看")
        else:
            return {"status": "unsupported", "reason": "仅支持PDF、DOCX、DOC附件", "locations": [], "candidates": candidates([], path.name)}
    except InterruptedError:
        raise
    except Exception as exc:
        return {"status": "error", "reason": f"解析失败：{type(exc).__name__}: {exc}", "locations": [], "candidates": candidates([], path.name)}
    return {"status": status, "reason": "；".join(warnings), "locations": locations, "candidates": candidates(locations, path.name)}
