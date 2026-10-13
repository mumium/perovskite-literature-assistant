import copy
import json
import urllib.request
import urllib.error
from pathlib import Path
import pytest

from perovskite import network
from perovskite.store import Store
from perovskite.selftest import fixtures, result_fixture
from perovskite.exchange import strict_json

def test_crossref_dates_do_not_use_registration(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({"message": {"title":["Test"], "published-online":{"date-parts":[[2024]]}, "published-print":{"date-parts":[[2025,3]]}, "published":{"date-parts":[[2024]]}, "created":{"date-parts":[[2023,1,2]]}, "indexed":{"date-parts":[[2026,10,13]]}, "author":[{"given":"A", "family":"B"}]}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a,**k: Response())
    result = network.crossref("10.9999/test")
    assert result["online_date"] == "2024" and result["print_date"] == "2025-03"
    assert "corresponding_authors" not in result

def test_api_chunks_resume_validation_and_secret_exclusion(tmp_path, monkeypatch):
    files = fixtures(tmp_path / "source")
    with Store(tmp_path / "data") as store:
        imported = {k:store.import_file(files[k]) for k in ["main", "si_pdf", "si_docx"]}
        aid = imported["main"]["article_id"]
        for k, role in [("main","main"),("si_pdf","si"),("si_docx","si")]:
            store.associate(imported[k]["file_id"], aid, role)
        calls = []
        def fake_chat(config, key, messages, cancel, progress):
            assert key == "SECRET_TEST_KEY"
            calls.append(messages)
            user = strict_json(messages[-1]["content"])
            if isinstance(user, list):
                return {"facts": user}, {"total_tokens":123}
            manifest_path = next((store.root / "api_jobs").rglob("manifest.json"))
            manifest = strict_json(manifest_path.read_text(encoding="utf-8"))
            return result_fixture(store, manifest), {"total_tokens":456}
        monkeypatch.setattr(network, "chat", fake_chat)
        cfg = {"api_enabled":True, "base_url":"https://api.example.invalid/v1", "model":"test", "timeout":10}
        result = network.analyze(store, aid, cfg, "SECRET_TEST_KEY", lambda:False, lambda *a:None)
        assert Path(result["result_path"]).exists()
        assert len(calls)==2
        again = network.analyze(store, aid, cfg, "SECRET_TEST_KEY", lambda:False, lambda *a:None)
        assert again["resumed"] and len(calls)==2
        for path in store.root.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".sqlite"}:
                assert b"SECRET_TEST_KEY" not in path.read_bytes()
        cfg["api_enabled"] = False
        with pytest.raises(ValueError, match="默认关闭"):
            network.analyze(store, aid, cfg, "SECRET_TEST_KEY", lambda:False, lambda *a:None)

def test_api_disallows_credentials_in_url_and_insecure_remote():
    base = {"model":"test", "timeout":5}
    for url in ["http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"]:
        with pytest.raises(ValueError):
            network.chat({**base,"base_url":url}, "secret", [], lambda:False, lambda *a:None)

def test_openai_compatible_transport_retry_usage_and_cancel(monkeypatch):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self):
            return json.dumps({"choices":[{"message":{"content":'{"answer":42}'}}], "usage":{"total_tokens":17}}).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            payload = json.loads(request.data)
            assert payload["response_format"] == {"type":"json_object"}
            assert request.get_header("Authorization") == "Bearer secret"
            assert request.full_url.endswith("/v1/chat/completions")
            if len(calls)==1:
                raise urllib.error.HTTPError(request.full_url,429,"rate limit",{},None)
            return Response()
    monkeypatch.setattr(urllib.request,"build_opener",lambda *a:Opener())
    monkeypatch.setattr(network.time,"sleep",lambda *a:None)
    config={"base_url":"https://example.invalid/v1","model":"test","timeout":5}
    content, usage=network.chat(config,"secret",[{"role":"user","content":"test"}],lambda:False,lambda *a:None)
    assert content=={"answer":42} and usage["total_tokens"]==17 and len(calls)==2
    with pytest.raises(InterruptedError):
        network.chat(config,"secret",[],lambda:True,lambda *a:None)
