"""Portable SQLite repository. All persisted paths are relative to the data root."""
import copy
import json
import os
import math
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path, PurePosixPath

from .extract import digest, extract, candidates
from .schema import META_FIELDS, empty_metadata

DB_VERSION = 2

def uid():
    return str(uuid.uuid4())

def now():
    return datetime.now(timezone.utc).isoformat()

def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(dumps(value), encoding="utf-8")
    os.replace(temporary, path)

def validate_metadata(meta):
    if not isinstance(meta, dict) or set(meta) - set(META_FIELDS):
        raise ValueError("元数据必须是对象，且只使用已定义字段")
    arrays = {"authors", "first_authors", "co_first_authors", "corresponding_authors", "affiliations", "tags"}
    for k, v in meta.items():
        if k in arrays:
            if not isinstance(v, list) or any(not isinstance(s, str) for s in v):
                raise ValueError(f"{k}必须是字符串数组")
        elif k == "si_complete":
            if v is not None and type(v) is not bool:
                raise ValueError("si_complete必须为true/false/null")
        elif v is not None and not isinstance(v, str):
            raise ValueError(f"{k}必须为字符串或null（年份请用字符串）")
        if k in {"online_date", "print_date", "accepted_date"} and v:
            if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", v):
                raise ValueError("日期请保留原精度：YYYY、YYYY-MM或YYYY-MM-DD")
            datetime.strptime(v, {4: "%Y", 7: "%Y-%m", 10: "%Y-%m-%d"}[len(v)])
    return meta

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        unpacked = getattr(sys, "_MEIPASS", None)
        if unpacked and self.root.is_relative_to(Path(unpacked).resolve()):
            raise ValueError("数据目录不能位于exe临时解包目录，请选择独立持久目录")
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ["files", "exchanges", "results", "api_jobs"]:
            (self.root / folder).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        existing_version = version
        if version > DB_VERSION:
            self.db.close()
            raise ValueError("数据目录来自更新版本程序，请使用更新版打开")
        if version == 0:
            self.db.executescript("""
            BEGIN;
            CREATE TABLE articles(article_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, candidates TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
            CREATE TABLE files(file_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, extracted TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE links(file_id TEXT PRIMARY KEY REFERENCES files(file_id), article_id TEXT NOT NULL REFERENCES articles(article_id), role TEXT NOT NULL, pairing TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE proposals(proposal_id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(file_id), article_id TEXT NOT NULL REFERENCES articles(article_id), reason TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE sections(article_id TEXT NOT NULL REFERENCES articles(article_id), name TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL, checks TEXT NOT NULL, human_verified INTEGER NOT NULL DEFAULT 0, modified TEXT NOT NULL, PRIMARY KEY(article_id,name));
            CREATE TABLE history(history_id TEXT PRIMARY KEY, article_id TEXT NOT NULL, scope TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL, source TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE packages(package_id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(article_id), manifest TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE imports(import_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, article_id TEXT, payload TEXT NOT NULL, report TEXT NOT NULL, created TEXT NOT NULL);
            CREATE VIRTUAL TABLE search_index USING fts5(article_id UNINDEXED, content, tokenize='unicode61');
            PRAGMA user_version=1;
            COMMIT;
            """)
            version = 1
        if version == 1:
            # Keep a consistent pre-upgrade copy in the portable data directory.
            if existing_version == 1:
                folder = self.root / "migrations"
                folder.mkdir(exist_ok=True)
                with closing(sqlite3.connect(folder / f"library-v1-{uid()}.sqlite")) as backup:
                    self.db.backup(backup)
            try:
                self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE highlights(mark_id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(file_id), file_sha256 TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, anchors TEXT NOT NULL, color TEXT NOT NULL, note TEXT NOT NULL, created TEXT NOT NULL, modified TEXT NOT NULL);
                CREATE INDEX highlights_file ON highlights(file_id);
                CREATE TABLE reader_state(file_id TEXT PRIMARY KEY REFERENCES files(file_id), page_index INTEGER NOT NULL, zoom INTEGER NOT NULL, zoom_mode TEXT NOT NULL, modified TEXT NOT NULL);
                PRAGMA user_version=2;
                COMMIT;
                """)
            except Exception:
                self.db.rollback()
                self.db.close()
                raise
        if not (self.root / "config.json").exists():
            atomic_json(self.root / "config.json", {"api_enabled": False, "base_url": "https://api.openai.com/v1", "model": "", "timeout": 60})

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def rows(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def article(self, article_id):
        row = self.db.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
        if not row:
            raise ValueError("文章不存在")
        row = dict(row)
        row["metadata"] = json.loads(row["metadata"])
        row["candidates"] = json.loads(row["candidates"])
        return row

    def files(self, article_id):
        return self.rows("SELECT f.*,l.role,l.pairing,l.confirmed FROM files f JOIN links l USING(file_id) WHERE l.article_id=? ORDER BY f.created", (article_id,))

    def file(self, file_id):
        row = self.db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        if not row:
            raise ValueError("附件不存在")
        return dict(row)

    def path(self, file_id):
        p = (self.root / self.file(file_id)["path"]).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("附件路径越过数据目录")
        return p

    def section(self, article_id, name):
        r = self.db.execute("SELECT * FROM sections WHERE article_id=? AND name=?", (article_id, name)).fetchone()
        if not r:
            return None
        r = dict(r)
        for field in ["content", "evidence", "checks"]:
            r[field] = json.loads(r[field])
        return r

    def new_article(self, metadata=None, candidates=None):
        article_id = uid()
        meta = empty_metadata()
        meta.update(metadata or {})
        self.db.execute("INSERT INTO articles VALUES(?,?,?,?,?)", (article_id, dumps(meta), dumps(candidates or []), "", now()))
        return article_id

    def history(self, article_id, scope, old, new, source):
        self.db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?)", (uid(), article_id, scope, dumps(old), dumps(new), source, now()))

    def reindex(self, article_id):
        a = self.article(article_id)
        pieces = [dumps(a["metadata"]), a["notes"]]
        pieces += [f["extracted"] for f in self.files(article_id)]
        pieces += [r["content"] for r in self.rows("SELECT content FROM sections WHERE article_id=?", (article_id,))]
        pieces += [r["text"] + "\n" + r["note"] for r in self.rows("SELECT h.text,h.note FROM highlights h JOIN links l USING(file_id) WHERE l.article_id=?", (article_id,))]
        self.db.execute("DELETE FROM search_index WHERE article_id=?", (article_id,))
        self.db.execute("INSERT INTO search_index VALUES(?,?)", (article_id, "\n".join(pieces)))

    def list_articles(self, query="", filters=None):
        filters = filters or {}
        if query.strip():
            phrase = '"' + query.replace('"', '""') + '"'
            ids = {r["article_id"] for r in self.rows("SELECT article_id FROM search_index WHERE search_index MATCH ? UNION SELECT article_id FROM search_index WHERE content LIKE ? ESCAPE '\\'", (phrase, "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"))}
        else:
            ids = None
        result = []
        for row in self.rows("SELECT * FROM articles ORDER BY created DESC"):
            if ids is not None and row["article_id"] not in ids:
                continue
            row["metadata"] = json.loads(row["metadata"])
            meta = row["metadata"]
            if any(str(value).casefold() not in str(meta.get(field) or "").casefold() for field, value in filters.items() if value and field != "state"):
                continue
            files = self.files(row["article_id"])
            sections = [s for n in ["story", "process"] if (s := self.section(row["article_id"], n))]
            if not files and not sections and not row["notes"]:
                continue  # Retain merged article history in the DB without empty duplicate UI rows.
            state = []
            if any(not f["confirmed"] for f in files):
                state.append("待配对确认")
            if any(f["status"] != "ready" for f in files):
                state.append("解析需关注")
            state += ["故事已导入" if any(s["name"] == "story" for s in sections) else "故事待分析", "工艺已导入" if any(s["name"] == "process" for s in sections) else "工艺待分析"]
            if any(any(not c["matched"] for c in s["checks"]) for s in sections):
                state.append("引用待核验")
            state.append("人工已核验" if len(sections) == 2 and all(s["human_verified"] for s in sections) else "人工待核验")
            row["state"] = " / ".join(state)
            if filters.get("state") and filters["state"] not in row["state"]:
                continue
            result.append(row)
        return result

    def import_file(self, source, cancel=lambda: False):
        source = Path(source)
        sha = digest(source, cancel)
        prior = self.db.execute("SELECT file_id,status FROM files WHERE sha256=?", (sha,)).fetchone()
        if prior:
            return {"file_id": prior["file_id"], "status": "duplicate", "reason": "哈希相同，跳过；失败附件可用重试解析"}
        file_id = uid()
        relative = f"files/{sha[:2]}/{file_id}{source.suffix.lower()}"
        target = self.root / relative
        target.parent.mkdir(exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".copying")
        try:
            with source.open("rb") as src, temporary.open("wb") as dst:
                while block := src.read(1024 * 1024):
                    if cancel():
                        raise InterruptedError("已取消")
                    dst.write(block)
            if digest(temporary, cancel) != sha:
                raise ValueError("复制期间源文件变化，未导入；请重试")
            os.replace(temporary, target)
            extracted = extract(target, cancel)
            extracted["candidates"] = candidates(extracted["locations"], source.name)
            candidate = extracted["candidates"]
            with self.db:
                self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", (file_id, sha, source.name, relative, extracted["status"], extracted["reason"], dumps(extracted), now()))
                # Explicit DOI is still only a candidate; all suggested pairings require confirmation.
                matches = []
                for article in self.rows("SELECT article_id,metadata FROM articles"):
                    meta = json.loads(article["metadata"])
                    if candidate["doi"] and meta.get("doi") == candidate["doi"]:
                        matches.append((article["article_id"], "首页DOI候选一致；同DOI版本仍保留，请确认角色"))
                    elif candidate["title"] and len(candidate["title"]) > 20 and (meta.get("title") or "").casefold() == candidate["title"].casefold():
                        author_overlap = set(candidate.get("authors", [])) & set(meta.get("authors", []))
                        matches.append((article["article_id"], "首页标题候选一致；" + ("显式作者标识有交集" if author_overlap else "作者身份需人工核验")))
                    elif candidate["role"] == "si":
                        source_names = [Path(f["name"]).stem.casefold() for f in self.files(article["article_id"]) if f["role"] == "main"]
                        stem = re.sub(r"(?i)(?:supporting|supplementary|(?:^|[ _.-])si(?:[ _.-]|$)|补充|支持信息)", "", source.stem).strip(" _.-").casefold()
                        if len(stem) >= 12 and stem in source_names:
                            matches.append((article["article_id"], "文件名主干一致；弱依据，需确认首页标题与作者"))
                new_id = self.new_article({"title": candidate["title"], "doi": candidate["doi"]}, [{"source": file_id, "location": "首页/前置段落", "values": candidate}])
                self.db.execute("INSERT INTO links VALUES(?,?,?,?,?)", (file_id, new_id, candidate["role"], candidate["basis"], 0))
                for article_id, reason in matches:
                    self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,0)", (uid(), file_id, article_id, reason))
                self.reindex(new_id)
            return {"file_id": file_id, "article_id": new_id, "status": extracted["status"], "reason": extracted["reason"], "pairing_candidates": len(matches)}
        except Exception:
            if not self.db.execute("SELECT 1 FROM files WHERE file_id=?", (file_id,)).fetchone():
                target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def retry(self, file_id, cancel=lambda: False):
        if digest(self.path(file_id), cancel) != self.file(file_id)["sha256"]:
            raise ValueError("受管理附件哈希变化，请从原件重新导入；未覆盖现有文本")
        value = extract(self.path(file_id), cancel)
        value["candidates"] = candidates(value["locations"], self.file(file_id)["name"])
        with self.db:
            self.db.execute("UPDATE files SET status=?,reason=?,extracted=? WHERE file_id=?", (value["status"], value["reason"], dumps(value), file_id))
            row = self.db.execute("SELECT article_id FROM links WHERE file_id=?", (file_id,)).fetchone()
            if row:
                self.reindex(row["article_id"])
        return value

    def associate(self, file_id, article_id, role, reason="人工关联"):
        if role not in {"main", "si", "version", "preprint", "other"}:
            raise ValueError("未知文件角色")
        self.article(article_id)
        self.file(file_id)
        prior = self.db.execute("SELECT * FROM links WHERE file_id=?", (file_id,)).fetchone()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO links VALUES(?,?,?,?,1)", (file_id, article_id, role, reason))
            self.db.execute("UPDATE proposals SET resolved=1 WHERE file_id=?", (file_id,))
            self.history(article_id, "links", dict(prior) if prior else None, {"file_id": file_id, "role": role}, reason)
            target = self.article(article_id)
            candidate = {"source": file_id, "location": "首页/前置段落", "values": json.loads(self.file(file_id)["extracted"])["candidates"]}
            if candidate not in target["candidates"]:
                target["candidates"].append(candidate)
                self.db.execute("UPDATE articles SET candidates=? WHERE article_id=?", (dumps(target["candidates"]), article_id))
            affected = {article_id, prior["article_id"]} if prior else {article_id}
            for aid in affected:
                # Membership changes invalidate verification, but retain all content and its history.
                for section in self.rows("SELECT name,checks FROM sections WHERE article_id=?", (aid,)):
                    checks = json.loads(section["checks"])
                    for check in checks:
                        check.update({"matched": False, "human_verified": False, "reason": "文件关联或角色变动，需重新检查来源及人工核验"})
                    self.db.execute("UPDATE sections SET human_verified=0,checks=? WHERE article_id=? AND name=?", (dumps(checks), aid, section["name"]))
                self.reindex(aid)

    def detach(self, file_id):
        with self.db:
            value = json.loads(self.file(file_id)["extracted"])["candidates"]
            aid = self.new_article({"title": value["title"], "doi": value["doi"]})
            self.associate(file_id, aid, value["role"], "人工解除关联并创建独立文献")
        return aid

    def save_metadata(self, article_id, metadata, notes):
        validate_metadata(metadata)
        old = self.article(article_id)
        with self.db:
            self.history(article_id, "metadata", {"metadata": old["metadata"], "notes": old["notes"]}, {"metadata": metadata, "notes": notes}, "人工修订")
            self.db.execute("UPDATE articles SET metadata=?,notes=? WHERE article_id=?", (dumps(metadata), notes, article_id))
            self.reindex(article_id)

    def add_candidates(self, article_id, values, source):
        old = self.article(article_id)
        old["candidates"].append({"source": source, "values": values, "retrieved": now()})
        with self.db:
            self.db.execute("UPDATE articles SET candidates=? WHERE article_id=?", (dumps(old["candidates"]), article_id))
            self.history(article_id, "metadata_candidates", None, values, source)

    def save_section(self, article_id, name, content, evidence, checks, source, verified=False):
        old = self.section(article_id, name)
        self.history(article_id, name, old, {"content": content, "evidence": evidence, "checks": checks, "human_verified": verified}, source)
        self.db.execute("INSERT OR REPLACE INTO sections VALUES(?,?,?,?,?,?,?)", (article_id, name, dumps(content), dumps(evidence), dumps(checks), int(verified), now()))
        self.reindex(article_id)

    def highlights(self, file_id):
        rows = self.rows("SELECT * FROM highlights WHERE file_id=? ORDER BY created", (file_id,))
        for row in rows:
            row["anchors"] = json.loads(row["anchors"])
        return rows

    def save_highlight(self, file_id, kind, text, anchors, color="yellow", note="", mark_id=None):
        file = self.file(file_id)
        if kind not in {"pdf", "docx"} or not file["path"].lower().endswith("." + kind):
            raise ValueError("高亮类型与附件不符")
        if color not in {"yellow", "green", "blue", "pink"}:
            raise ValueError("未知高亮颜色")
        if not isinstance(text, str) or not text.strip() or not isinstance(note, str):
            raise ValueError("请先选择需要高亮的原文")
        if not isinstance(anchors, list) or not anchors or len(anchors) > 10000:
            raise ValueError("高亮位置为空或过多")
        locs = {x["loc_id"]: x["text"] for x in json.loads(file["extracted"])["locations"]}
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise ValueError("高亮位置格式错误")
            if kind == "pdf":
                rect = anchor.get("rect")
                if set(anchor) != {"page_index", "rect"} or type(anchor["page_index"]) is not int or anchor["page_index"] < 0:
                    raise ValueError("PDF实际页面索引无效")
                if not isinstance(rect, list) or len(rect) != 4 or any(type(n) not in {int, float} or not math.isfinite(n) for n in rect) or rect[2] <= rect[0] or rect[3] <= rect[1]:
                    raise ValueError("PDF高亮坐标无效")
                if not any(x.get("page_index") == anchor["page_index"] for x in json.loads(file["extracted"])["locations"]):
                    raise ValueError("PDF高亮页不存在")
            else:
                if set(anchor) != {"loc_id", "start", "end"} or anchor["loc_id"] not in locs:
                    raise ValueError("DOCX高亮段落位置不存在")
                if type(anchor["start"]) is not int or type(anchor["end"]) is not int or not 0 <= anchor["start"] < anchor["end"] <= len(locs[anchor["loc_id"]]):
                    raise ValueError("DOCX高亮字符范围无效")
        if kind == "docx" and "\n".join(locs[a["loc_id"]][a["start"]:a["end"]] for a in anchors) != text:
            raise ValueError("DOCX高亮原文与位置不一致")
        old = None
        if mark_id:
            old = self.db.execute("SELECT * FROM highlights WHERE mark_id=? AND file_id=?", (mark_id, file_id)).fetchone()
            if not old:
                raise ValueError("高亮不存在或不属于当前附件")
            old = dict(old)
        mark_id = mark_id or uid()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO highlights VALUES(?,?,?,?,?,?,?,?,?,?)", (mark_id, file_id, file["sha256"], kind, text, dumps(anchors), color, note, old["created"] if old else now(), now()))
            row = self.db.execute("SELECT article_id FROM links WHERE file_id=?", (file_id,)).fetchone()
            if row:
                self.history(row["article_id"], "highlights", old, {"mark_id": mark_id, "file_id": file_id, "text": text, "anchors": anchors, "color": color, "note": note}, "人工原文高亮")
                self.reindex(row["article_id"])
        return mark_id

    def delete_highlight(self, mark_id, file_id):
        row = self.db.execute("SELECT * FROM highlights WHERE mark_id=? AND file_id=?", (mark_id, file_id)).fetchone()
        if not row:
            raise ValueError("高亮不存在或不属于当前附件")
        with self.db:
            self.db.execute("DELETE FROM highlights WHERE mark_id=? AND file_id=?", (mark_id, file_id))
            article = self.db.execute("SELECT article_id FROM links WHERE file_id=?", (file_id,)).fetchone()
            if article:
                self.history(article["article_id"], "highlights", dict(row), {"deleted": mark_id}, "人工删除高亮")
                self.reindex(article["article_id"])

    def reader_state(self, file_id):
        row = self.db.execute("SELECT * FROM reader_state WHERE file_id=?", (file_id,)).fetchone()
        return dict(row) if row else {"page_index": 0, "zoom": 100, "zoom_mode": "fit"}

    def save_reader_state(self, file_id, page_index, zoom, zoom_mode):
        self.file(file_id)
        if type(page_index) is not int or page_index < 0 or type(zoom) is not int or not 25 <= zoom <= 400 or zoom_mode not in {"fit", "manual"}:
            raise ValueError("阅读位置或缩放比例无效")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO reader_state VALUES(?,?,?,?,?)", (file_id, page_index, zoom, zoom_mode, now()))

    def backup(self, target, cancel=lambda: False):
        target = Path(target).resolve()
        if target.is_relative_to(self.root):
            raise ValueError("备份请选择数据目录外的位置")
        temporary_db = self.root / ("backup-" + uid() + ".sqlite")
        partial = target.with_name(target.name + ".writing")
        try:
            with closing(sqlite3.connect(temporary_db)) as dest:
                self.db.backup(dest)
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temporary_db, "library.sqlite")
                for folder in ["files", "exchanges", "results", "api_jobs", "migrations", "pdf_exports", "pdf_backups", "annotated"]:
                    for path in (self.root / folder).rglob("*"):
                        if cancel():
                            raise InterruptedError("备份已取消")
                        if path.is_file() and not path.name.endswith((".writing", ".copying")):
                            archive.write(path, path.relative_to(self.root).as_posix())
                config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
                config.pop("api_key", None)
                archive.writestr("config.json", dumps(config))
            os.replace(partial, target)
        finally:
            temporary_db.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)

def restore(archive_path, destination):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("为保护现有数据，只能恢复到空目录")
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / "restore-staging"
    staging.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if "library.sqlite" not in archive.namelist():
                raise ValueError("备份缺少数据库")
            if sum(i.file_size for i in archive.infolist()) > 100 * 1024**3:
                raise ValueError("备份过大，超过100GB安全上限")
            for item in archive.infolist():
                relative = PurePosixPath(item.filename.replace("\\", "/"))
                target = (staging / relative).resolve()
                if relative.is_absolute() or not target.is_relative_to(staging.resolve()) or ":" in str(relative):
                    raise ValueError("备份含不安全路径")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        with Store(staging) as check:
            if check.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库损坏")
            for row in check.rows("SELECT file_id,sha256 FROM files"):
                if digest(check.path(row["file_id"])) != row["sha256"]:
                    raise ValueError("备份附件哈希不一致")
        for path in list(staging.iterdir()):
            path.rename(destination / path.name)
    finally:
        # Only our checked staging directory; never remove the destination or its original data.
        if staging.exists() and staging.resolve().is_relative_to(destination):
            shutil.rmtree(staging)
