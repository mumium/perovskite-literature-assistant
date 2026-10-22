"""Remember a data-directory locator without registry or temporary-bundle writes."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

STARTUP_FILE="startup-location.json"
DATA_FOLDER='钙钛矿文献助手数据'

def library_in_folder(selected):
    """Open existing libraries as-is; new libraries get a dedicated child folder."""
    if not selected: return ''
    path=Path(selected).resolve()
    if (path/'library.sqlite').is_file(): return str(path)
    child=path/DATA_FOLDER
    if child.exists() and not (child/'library.sqlite').is_file():
        # A pre-existing unrelated folder must not be adopted or overwritten.
        index=2
        while (path/f'{DATA_FOLDER} ({index})').exists(): index+=1
        child=path/f'{DATA_FOLDER} ({index})'
    return str(child)

class StartupMemory:
    def __init__(self,app_directory=None,fallback_directory=None):
        self.app_directory=Path(app_directory or (Path(sys.executable).parent if getattr(sys,"frozen",False) else Path(__file__).resolve().parent.parent)).resolve()
        self.fallback_directory=Path(fallback_directory or (Path(os.environ.get("LOCALAPPDATA") or Path.home()/"AppData"/"Local")/"PerovskiteLiteratureAssistant")).resolve()

    def last_directory(self):
        records=[]
        for base in [self.app_directory,self.fallback_directory]:
            try:
                with (base/STARTUP_FILE).open("rb") as stream: raw=stream.read(16385)
                if len(raw)>16384: continue
                record=json.loads(raw.decode("utf-8"))
                if not isinstance(record,dict) or record.get("schema_version")!=1: continue
                name=record.get("data_directory")
                stamp=record.get("saved_at_ns")
                if not isinstance(name,str) or not name or not isinstance(record.get("relative"),bool) or type(stamp) is not int: continue
                path=Path(name)
                if record["relative"]:
                    if path.is_absolute(): continue
                    path=base/path
                elif not path.is_absolute(): continue
                records.append((stamp,path.resolve()))
            except (OSError,ValueError,TypeError,UnicodeError):
                continue
        return max(records,key=lambda r:r[0])[1] if records else None

    def choose_directory(self,picker,explicit=None):
        if explicit: return str(explicit)
        remembered=self.last_directory()
        try:
            if remembered and (remembered/"library.sqlite").is_file(): return str(remembered)
        except OSError:
            pass
        title="选择存放位置：自动建立“钙钛矿文献助手数据”子文件夹；也可选择已有库"
        if remembered: title="上次数据目录不可用，请重新选择（原数据不会删除）"
        return library_in_folder(picker(None,title,str(remembered.parent) if remembered else ""))

    def remember(self,directory):
        directory=Path(directory).resolve()
        if not (directory/"library.sqlite").is_file():
            raise ValueError("只记忆已成功打开的文献数据目录")
        stamp=time.time_ns()
        for base in [self.app_directory,self.fallback_directory]:
            stage=None
            try:
                name=str(directory); relative=False
                if base==self.app_directory:
                    try: name=os.path.relpath(directory,base); relative=True
                    except ValueError: pass  # Different Windows drives.
                record={"schema_version":1,"data_directory":name,"relative":relative,"saved_at_ns":stamp}
                base.mkdir(parents=True,exist_ok=True)
                fd,name_stage=tempfile.mkstemp(prefix=".startup-",suffix=".json",dir=base)
                stage=Path(name_stage)
                with os.fdopen(fd,"w",encoding="utf-8") as stream:
                    json.dump(record,stream,ensure_ascii=False,indent=2)
                os.replace(stage,base/STARTUP_FILE)
                return base/STARTUP_FILE
            except OSError:
                continue
            finally:
                if stage:
                    try: stage.unlink(missing_ok=True)
                    except OSError: pass
        raise OSError("无法保存上次数据目录位置；下次启动需重新选择")
