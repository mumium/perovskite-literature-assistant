"""No network calls on import/startup. Caller must explicitly enable and invoke."""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .exchange import export_package, strict_json, validate_result
from .schema import PROMPT, RESULT_SCHEMA, template
from .store import atomic_json, dumps

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API服务返回重定向，已停止以保护认证信息；请设置最终服务地址")

def crossref(doi, timeout=30):
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": "PerovskiteLiteratureAssistant/0.1 (user-initiated metadata lookup)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        msg = json.load(response)["message"]
    def date(field):
        parts = msg.get(field, {}).get("date-parts", [[]])[0]
        return "-".join([str(parts[0])] + [f"{x:02d}" for x in parts[1:]]) if parts else None
    return {"title": (msg.get("title") or [None])[0], "journal": (msg.get("container-title") or [None])[0],
            "doi": msg.get("DOI"), "volume": msg.get("volume"), "issue": msg.get("issue"), "pages": msg.get("page") or msg.get("article-number"),
            "authors": [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in msg.get("author", [])],
            "online_date": date("published-online"), "print_date": date("published-print"), "year": (date("published") or "")[:4] or None,
            "publisher_url": msg.get("URL"), "source_note": "Crossref候选；未使用created/deposited/indexed日期，不推断通讯及共同第一作者"}

def chat(config, key, messages, cancel, progress):
    base = config["base_url"].rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("API地址需HTTPS，本机服务允许HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API地址不应包含凭据、查询参数或片段")
    if not key.strip() or not config["model"].strip():
        raise ValueError("请填写模型名和API Key；ChatGPT订阅不等于API权限")
    body = dumps({"model": config["model"], "messages": messages, "response_format": {"type": "json_object"}, "stream": False}).encode("utf-8")
    for attempt in range(3):
        if cancel():
            raise InterruptedError("API任务已取消，已完成块保留")
        request = urllib.request.Request(base + "/chat/completions", data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=int(config["timeout"])) as response:
                result = json.load(response)
            if cancel():
                raise InterruptedError("API任务已取消，已完成块保留")
            content = result["choices"][0]["message"]["content"]
            return strict_json(content), result.get("usage", {})
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise ValueError(f"API HTTP {exc.code}；检查服务地址、权限和模型（响应正文未记录以避免泄露）") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise ValueError("API连接/超时失败；可重试并续用已完成块") from None
        progress(0, 0, f"请求失败，准备第{attempt+2}次尝试")
        for _ in range(10 * (attempt+1)):
            if cancel():
                raise InterruptedError("已取消")
            time.sleep(.1)

def analyze(store, article_id, config, key, cancel, progress):
    if not config.get("api_enabled"):
        raise ValueError("API默认关闭，请先明确开启")
    files = store.files(article_id)
    signature = hashlib.sha256(dumps({"article_id": article_id, "files": [(f["file_id"],f["sha256"],f["role"]) for f in files], "model": config["model"], "base_url": config["base_url"], "prompt": PROMPT}).encode()).hexdigest()
    folder = store.root / "api_jobs" / signature
    folder.mkdir(exist_ok=True)
    if (folder / "manifest.json").exists():
        manifest = strict_json((folder / "manifest.json").read_text(encoding="utf-8"))
    else:
        manifest = export_package(store, article_id, folder / "analysis.zip", cancel)
        atomic_json(folder / "manifest.json", manifest)
    if (folder / "result.json").exists():
        value = strict_json((folder / "result.json").read_text(encoding="utf-8"))
        validate_result(store, value)
        return {"result_path": str(folder / "result.json"), "usage": strict_json((folder / "usage.json").read_text(encoding="utf-8")) if (folder / "usage.json").exists() else [], "resumed": True}
    chunks, current = [], []
    size = 0
    for f in manifest["files"]:
        for loc in f["locations"]:
            value = {"file_id": f["file_id"], "role": f["role"], **loc}
            length = len(dumps(value))
            if size + length > 26000 and current:
                chunks.append(current)
                current, size = [], 0
            current.append(value)
            size += length
    if current:
        chunks.append(current)
    if not chunks:
        raise ValueError("附件无可读文本")
    notes, usage = [], []
    for i, chunk in enumerate(chunks):
        if cancel():
            raise InterruptedError("已取消；下次启动分析从未完成块继续")
        progress(i, len(chunks)+1, f"提取文本块 {i+1}/{len(chunks)}")
        path = folder / f"chunk-{i}.json"
        if path.exists():
            saved = strict_json(path.read_text(encoding="utf-8"))
        else:
            content, used = chat(config, key, [{"role": "system", "content": PROMPT + "\n当前为分块提取阶段。返回JSON对象notes（记录事实、样品、条件、缺失项、机制、逐字引文file_id/loc_id），不在此阶段写最终故事。"}, {"role": "user", "content": dumps(chunk)}], cancel, progress)
            if not isinstance(content, dict) or not content:
                raise ValueError("分块返回不是有效非空JSON对象")
            saved = {"notes": content, "usage": used}
            atomic_json(path, saved)
        notes.append(saved["notes"])
        usage.append(saved["usage"])
    if len(dumps(notes)) > 180000:
        raise ValueError("综合笔记超过180000字符，未截断；请使用离线分析包分阶段综合")
    progress(len(chunks), len(chunks)+1, "综合所有文本块，生成结构化结果")
    envelope = template(manifest["package_id"], article_id, manifest["files"])
    content, used = chat(config, key, [{"role": "system", "content": PROMPT + "\n这是最终综合阶段，输出符合提供schema的JSON。"}, {"role": "user", "content": dumps({"template": envelope, "schema": RESULT_SCHEMA, "metadata": manifest["metadata"], "files_status": [{"file_id": f["file_id"], "status": f["status"], "reason": f["reason"]} for f in manifest["files"]], "all_chunk_notes": notes})}], cancel, progress)
    # Keep invalid replies for explicit repair/retry without falsely marking success.
    atomic_json(folder / "last-response.json", content)
    validate_result(store, content)
    atomic_json(folder / "result.json", content)
    usage.append(used)
    atomic_json(folder / "usage.json", usage)
    progress(len(chunks)+1, len(chunks)+1, "分析完成；等待导入预览与人工核验")
    return {"result_path": str(folder / "result.json"), "usage": usage}
