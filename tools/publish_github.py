"""Developer-only public publishing. Requires an explicit --publish-public flag.

Uses the configured Git credential helper; credentials are never printed or saved.
The desktop application never imports this module or contacts GitHub.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def git(*args, binary=False):
    env=dict(os.environ,GIT_TERMINAL_PROMPT="0",GCM_INTERACTIVE="Never")
    # Keep the user's configured proxy: GitHub's git endpoint may need it even
    # when api.github.com can be reached directly.
    result=subprocess.run(["git",*args],cwd=ROOT,env=env,capture_output=True,timeout=180)
    if result.returncode:
        raise RuntimeError("git " + args[0] + " failed: " + result.stderr.decode("utf-8",errors="replace")[:1000])
    return result.stdout if binary else result.stdout.decode("utf-8",errors="strict").strip()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise RuntimeError("GitHub API redirect rejected")

class GitHub:
    def __init__(self):
        env=dict(os.environ,GIT_TERMINAL_PROMPT="0",GCM_INTERACTIVE="Never")
        credential=subprocess.run(["git","credential","fill"],input="protocol=https\nhost=github.com\n\n",text=True,capture_output=True,env=env,timeout=30)
        values=dict(line.split("=",1) for line in credential.stdout.splitlines() if "=" in line)
        if credential.returncode or not values.get("password"):
            raise RuntimeError("GitHub authentication is unavailable in the Git credential helper")
        self.key=values["password"]
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def request(self,path,method="GET",payload=None,data=None,content_type=None,missing_ok=False):
        url=path if path.startswith("https://") else "https://api.github.com"+path
        parsed=urllib.parse.urlparse(url)
        if parsed.scheme!="https" or parsed.hostname not in {"api.github.com","uploads.github.com"}:
            raise RuntimeError("Unexpected GitHub API host")
        if payload is not None: data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        headers={"Authorization":"Bearer "+self.key,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"Perovskite-Literature-Assistant-Publisher"}
        if data is not None: headers["Content-Type"]=content_type or "application/json"
        attempts=3 if method=="GET" else 1
        for attempt in range(attempts):
            request_url=url
            if method=="GET" and attempt:
                separator="&" if parsed.query else "?"
                request_url=url+separator+"_retry="+str(time.time_ns())
            try:
                with self.opener.open(urllib.request.Request(request_url,data=data,method=method,headers=headers),timeout=180) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if missing_ok and exc.code==404: return None
                if exc.code in {500,502,503,504} and attempt+1<attempts:
                    time.sleep(attempt+1); continue
                raise RuntimeError(f"GitHub API returned HTTP {exc.code} for {method} {parsed.path}") from None
            except (urllib.error.URLError,ConnectionError,TimeoutError):
                if attempt+1<attempts:
                    time.sleep(attempt+1); continue
                raise RuntimeError(f"GitHub network connection failed for {method} {parsed.path}") from None

def audit_index():
    names=git("diff","--cached","--name-only","-z",binary=True).decode("utf-8").split("\0")
    secret=re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,}|sk-[A-Za-z0-9_-]{24,})")
    forbidden={".venv","dist","build","test-output","tmp","files","api_jobs","exchanges","results","migrations","pdf_exports","pdf_backups","annotated"}
    for name in filter(None,names):
        path=Path(name)
        if set(path.parts)&forbidden or path.suffix.lower() in {".sqlite",".db",".pdf",".docx",".doc"} or path.name in {"config.json","startup-location.json",".env"} or path.name.startswith(".startup-"):
            raise RuntimeError("Refusing to publish data/runtime file: "+name)
        contents=git("show",":"+name,binary=True)
        if secret.search(contents): raise RuntimeError("Possible credential found in staged file: "+name)
        if str(Path.home()).encode("utf-8") in contents or re.search(rb"\b\d{5,12}@qq\.com\b",contents):
            raise RuntimeError("Local personal information found in staged file: "+name)
    return len(list(filter(None,names)))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--repository",required=True,help="owner/name")
    parser.add_argument("--tag",default="v0.8.3")
    parser.add_argument("--publish-public",action="store_true")
    parser.add_argument("--commit-message",default="Update portable literature assistant")
    args=parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",args.repository): raise RuntimeError("Invalid repository name")
    api=GitHub(); user=api.request("/user")
    print("Authenticated GitHub account:",user["login"],flush=True)
    owner,name=args.repository.split("/")
    if owner!=user["login"]: raise RuntimeError("Publishing only to the authenticated personal account is supported")
    repo=api.request("/repos/"+args.repository,missing_ok=True)
    if not args.publish_public:
        print(json.dumps({"login":user["login"],"repository":args.repository,"available":repo is None,"existing_public":bool(repo and not repo["private"])},ensure_ascii=False)); return
    report=json.loads((ROOT/"dist/验收报告.json").read_text(encoding="utf-8"))
    import hashlib
    if not report.get("passed") or hashlib.sha256((ROOT/"dist/钙钛矿文献助手.exe").read_bytes()).hexdigest()!=report["sha256"]:
        raise RuntimeError("Current Windows executable has not passed acceptance")
    remotes=git("remote","-v")
    if repo and (repo["private"] or (repo["size"] and args.repository not in remotes)):
        raise RuntimeError("Repository already exists and is not this project's public remote; choose a new name")
    git("add","--all")
    file_count=audit_index()
    print("Audited source files:",file_count,flush=True)
    if not repo:
        repo=api.request("/user/repos","POST",payload={"name":name,"description":"钙钛矿文献助手：离线便携Windows文献管理、正文/SI分析包交换、工艺证据与原文高亮。","private":False,"auto_init":False,"has_issues":True,"has_wiki":False})
        print("Created public repository:",repo["html_url"],flush=True)
    if "origin" not in git("remote").splitlines(): git("remote","add","origin",repo["clone_url"])
    git("config","user.name",user["login"])
    git("config","user.email",f"{user['id']}+{user['login']}@users.noreply.github.com")
    if git("branch","--show-current")=="master": git("branch","-m","codex/portable-reader")
    if file_count: git("commit","-m",args.commit_message)
    commit=git("rev-parse","HEAD")
    branch=repo.get("default_branch") or "main"
    git("-c","http.version=HTTP/1.1","push","-u","origin",f"HEAD:{branch}")
    print("Pushed source:",commit,flush=True)
    api.request("/repos/"+args.repository,"PATCH",payload={"default_branch":branch})
    remote=api.request(f"/repos/{args.repository}/commits/{branch}")
    if remote["sha"]!=commit: raise RuntimeError("Remote commit does not match local HEAD")
    release=api.request(f"/repos/{args.repository}/releases/tags/{args.tag}",missing_ok=True)
    if not release:
        release=api.request(f"/repos/{args.repository}/releases","POST",payload={"tag_name":args.tag,"target_commitish":commit,"name":"钙钛矿文献助手 "+args.tag,"body":(ROOT/f"docs/RELEASE_{args.tag}.md").read_text(encoding="utf-8"),"draft":False,"prerelease":False})
    existing={a["name"]:a for a in api.request(f"/repos/{args.repository}/releases/{release['id']}/assets")}
    executable_name=f"perovskite-literature-assistant-{args.tag}-windows-x64.exe"
    source_name=f"perovskite-literature-assistant-{args.tag}-source.zip"
    artifacts={"钙钛矿文献助手.exe":executable_name,"钙钛矿文献助手-源码与说明.zip":source_name,"使用说明.md":"README-zh-CN.md","SHA256-GitHub.txt":"SHA256SUMS.txt","公开验收报告.json":"acceptance-summary.json"}
    (ROOT/"dist/SHA256-GitHub.txt").write_text("\n".join(hashlib.sha256((ROOT/"dist"/local).read_bytes()).hexdigest()+"  "+remote for local,remote in artifacts.items() if local.endswith((".exe",".zip")))+"\n",encoding="utf-8")
    uploaded=[]
    for local_name,name in artifacts.items():
        if name in existing:
            if existing[name]["size"]!=(ROOT/"dist"/local_name).stat().st_size: raise RuntimeError("Existing release asset differs; publish a new version instead: "+name)
            uploaded.append({"name":name,"url":existing[name]["browser_download_url"]})
            continue
        url=release["upload_url"].split("{")[0]+"?name="+urllib.parse.quote(name,safe="")
        asset=api.request(url,"POST",data=(ROOT/"dist"/local_name).read_bytes(),content_type="application/octet-stream")
        print("Uploaded:",asset["name"],asset["size"],flush=True)
        uploaded.append({"name":asset["name"],"url":asset["browser_download_url"]})
    published=api.request("/repos/"+args.repository)
    if published["private"]: raise RuntimeError("Repository is not public")
    summary={"repository":published["html_url"],"public":True,"commit":commit,"release":release["html_url"],"tag":args.tag,"assets":uploaded}
    (ROOT/"dist/GitHub发布记录.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(1)
