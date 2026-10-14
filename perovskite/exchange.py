import copy
import difflib
import hashlib
import json
import re
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator
from .extract import digest
from .schema import PROMPT, RESULT_SCHEMA, SCHEMA_VERSION, STORY_FIELDS, template
from .store import atomic_json, dumps, now, uid

def strict_json(text):
    def no_constants(value):
        raise ValueError(f"非法JSON常量：{value}")
    def unique_pairs(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError(f"JSON含重复字段：{k}")
            result[k] = v
        return result
    return json.loads(text, parse_constant=no_constants, object_pairs_hook=unique_pairs)

def normalized(text):
    return re.sub(r"\s+", "", text.replace("\u00ad", "").replace("ﬁ", "fi").replace("ﬂ", "fl"))

def export_package(store, article_id, target, cancel=lambda: False):
    article = store.article(article_id)
    files = store.files(article_id)
    if not files:
        raise ValueError("文章没有附件，请先配对")
    package_id = uid()
    manifest = {"schema_version": SCHEMA_VERSION, "package_id": package_id, "article_id": article_id,
                "metadata": article["metadata"], "metadata_candidates": article["candidates"], "files": [], "created": now(),
                "limitations": "提取文本包含全部可读取位置；图片表格需对照原件，未内置OCR。未解析附件也列出状态。"}
    for f in files:
        if cancel():
            raise InterruptedError("导出已取消")
        if digest(store.path(f["file_id"]), cancel) != f["sha256"]:
            raise ValueError("受管理附件已改变，停止导出")
        extraction = json.loads(f["extracted"])
        manifest["files"].append({k: f[k] for k in ["file_id", "sha256", "name", "role", "status", "reason", "pairing"]} | {"locations": extraction["locations"]})
    result = template(package_id, article_id, files)
    path = Path(target)
    temporary = path.with_name(path.name + ".writing")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", dumps(manifest))
            archive.writestr("prompt.txt", PROMPT)
            archive.writestr("result-template.json", dumps(result))
            archive.writestr("result-schema.json", dumps(RESULT_SCHEMA))
            archive.writestr("README.txt", "这是文本分析包，无API Key和原始二进制文件。按manifest内全部locations逐块阅读。模板中的空数组不是分析结果。完成story或process后仅返回已完成的section。字段解释见prompt及JSON Schema。保留全部标识及哈希，将JSON导回原数据目录。")
        temporary.replace(path)
        with store.db:
            store.db.execute("INSERT INTO packages VALUES(?,?,?,?)", (package_id, article_id, dumps(manifest), now()))
        atomic_json(store.root / "exchanges" / f"{package_id}.json", manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest

def evidence_checks(store, article_id, evidence):
    files = {f["file_id"]: f for f in store.files(article_id)}
    checks = []
    seen = set()
    for e in evidence:
        if e["evidence_id"] in seen:
            raise ValueError("证据ID重复")
        seen.add(e["evidence_id"])
        if e["file_id"] not in files:
            raise ValueError("证据引用的附件不属于当前文章")
        locs = {x["loc_id"]: x for x in json.loads(files[e["file_id"]]["extracted"])["locations"]}
        if e["loc_id"] not in locs:
            raise ValueError(f"证据位置不存在：{e['loc_id']}")
        if e["loc_id"].startswith("docx:") and e["printed_page"] is not None:
            raise ValueError("DOCX不能填写印刷页码")
        quote = normalized(e["quote"])
        matched = bool(quote) and quote in normalized(locs[e["loc_id"]]["text"])
        checks.append({"evidence_id": e["evidence_id"], "matched": matched, "human_verified": False,
                       "reason": "原文引用匹配；结论仍待人工核验" if matched else "原文引用无法匹配，进入待核验"})
    return checks

def referenced_ids(value):
    ids = set()
    if isinstance(value, dict):
        ids.update(value.get("evidence_ids", []))
        for k, v in value.items():
            if k != "evidence_ids":
                ids.update(referenced_ids(v))
    elif isinstance(value, list):
        for v in value:
            ids.update(referenced_ids(v))
    return ids

def semantic_validate(sections, evidence):
    valid = {e["evidence_id"] for e in evidence}
    if referenced_ids(sections) - valid:
        raise ValueError("内容引用了未定义的证据ID")
    for claims in sections.get("story", {}).values():
        for c in claims:
            if c["kind"] != "ai_inference" and not c["evidence_ids"]:
                raise ValueError("作者主张/直接结果必须有证据")
    process = sections.get("process", {})
    ids = [s["sample_id"] for s in process.get("samples", [])]
    if len(ids) != len(set(ids)) or any(not s for s in ids):
        raise ValueError("样品ID为空或重复")
    record_ids = set()
    for rec in process.get("performance", []):
        if rec["sample_id"] not in ids:
            raise ValueError("性能记录关联的样品不存在")
        if rec["record_id"] in record_ids:
            raise ValueError("性能记录ID重复")
        record_ids.add(rec["record_id"])
        if not rec["evidence_ids"]:
            raise ValueError("性能记录必须关联证据")
    def walk(value):
        if isinstance(value, dict):
            if "original_value" in value:
                if value["original_value"] is not None and not value["evidence_ids"]:
                    raise ValueError("有值参数必须有证据")
                if value["normalized_value"] is not None:
                    if value["original_value"] is None or not value["normalized_unit"] or not value["conversion_note"]:
                        raise ValueError("标准化值必须保留原文值、标准单位和换算说明")
            if "category" in value and "parameters" in value and not value["evidence_ids"]:
                raise ValueError("工艺步骤必须关联证据")
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(sections)

def validate_result(store, value, verify_hashes=True):
    errors = list(Draft202012Validator(RESULT_SCHEMA).iter_errors(value))
    if errors:
        raise ValueError("结构错误：" + "; ".join(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors[:8]))
    store.article(value["article_id"])
    row = store.db.execute("SELECT manifest,article_id FROM packages WHERE package_id=?", (value["package_id"],)).fetchone()
    if not row or row["article_id"] != value["article_id"]:
        raise ValueError("分析包ID未登记或不属于本文章；请导回原数据目录")
    manifest = json.loads(row["manifest"])
    expected = {f["file_id"]: f["sha256"] for f in manifest["files"]}
    incoming = {f["file_id"]: f["sha256"] for f in value["file_hashes"]}
    current = {f["file_id"]: f["sha256"] for f in store.files(value["article_id"])}
    if len(incoming) != len(value["file_hashes"]) or incoming != expected or current != expected:
        raise ValueError("文件清单、哈希或当前配对变化，请重新导出分析包")
    if verify_hashes:
        for fid, sha in current.items():
            if digest(store.path(fid)) != sha:
                raise ValueError("附件实际哈希变化，结果拒绝关联")
    if not value["sections"]:
        raise ValueError("结果没有完成的故事或工艺段")
    semantic_validate(value["sections"], value["evidence"])
    checks = evidence_checks(store, value["article_id"], value["evidence"])
    warnings = []
    if any(not c["matched"] for c in checks):
        warnings.append("部分原文引用未匹配，只能作为待核验内容保存")
    if any(f["status"] != "ready" for f in manifest["files"]):
        warnings.append("本篇有未完整解析的附件，分析可能缺失信息")
    for name, content in value["sections"].items():
        if name == "story" and not any(content.values()):
            raise ValueError("故事全为空；未完成请省略story段")
        if name == "process" and not content["samples"]:
            raise ValueError("工艺没有样品；未完成请省略process段")
    return checks, warnings

def preview(store, value):
    checks, warnings = validate_result(store, value)
    changes = {}
    for name, content in value["sections"].items():
        old = store.section(value["article_id"], name)
        changes[name] = {"protected": bool(old and old["human_verified"]), "diff": "\n".join(difflib.unified_diff(
            dumps(old["content"] if old else {}).splitlines(), dumps(content).splitlines(), fromfile="现有内容", tofile="新结果", lineterm=""))}
    return {"article_id": value["article_id"], "checks": checks, "warnings": warnings, "changes": changes}

def apply_result(store, value, names=None):
    report = preview(store, value)
    sha = hashlib.sha256(dumps(value).encode()).hexdigest()
    names = set(value["sections"]) if names is None else set(names)
    if names - set(value["sections"]):
        raise ValueError("选择了不存在的结果段")
    previous = store.db.execute("SELECT import_id,report FROM imports WHERE sha256=?", (sha,)).fetchone()
    prior_report = json.loads(previous["report"]) if previous else {}
    completed = set(prior_report.get("applied", [])) | set(prior_report.get("protected", []))
    names -= completed
    if not names:
        return {"status": "duplicate", "reason": "所选JSON结果段已处理，未重复覆盖"}
    applied, protected = [], []
    with store.db:
        for name, content in value["sections"].items():
            if name not in names:
                continue
            if report["changes"][name]["protected"]:
                protected.append(name)
                continue
            used = referenced_ids(content)
            evidence = [e for e in value["evidence"] if e["evidence_id"] in used]
            checks = [c for c in report["checks"] if c["evidence_id"] in used]
            store.save_section(value["article_id"], name, content, evidence, checks, "分析包导回（人工待核验）")
            applied.append(name)
        report.update({"status": "applied" if applied else "candidate_only", "applied": sorted(set(applied) | set(prior_report.get("applied", []))), "protected": sorted(set(protected) | set(prior_report.get("protected", [])))})
        if previous:
            store.db.execute("UPDATE imports SET report=? WHERE import_id=?", (dumps(report), previous["import_id"]))
        else:
            store.db.execute("INSERT INTO imports VALUES(?,?,?,?,?,?)", (uid(), sha, value["article_id"], dumps(value), dumps(report), now()))
    atomic_json(store.root / "results" / f"{sha}.json", value)
    return report

def import_results(store, paths, cancel=lambda: False, progress=lambda *_: None):
    reports = []
    for index, path in enumerate(paths):
        if cancel():
            break
        try:
            value = strict_json(Path(path).read_text(encoding="utf-8-sig"))
            reports.append({"file": str(Path(path).name), **apply_result(store, value)})
        except Exception as exc:
            reports.append({"file": str(Path(path).name), "status": "error", "reason": str(exc)})
        progress(index+1, len(paths), Path(path).name)
    atomic_json(store.root / "results" / f"report-{uid()}.json", reports)
    return reports
