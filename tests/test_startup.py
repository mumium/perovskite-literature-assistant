import json
import shutil
from pathlib import Path
import pytest
from perovskite.startup import StartupMemory,STARTUP_FILE
from perovskite.store import Store

def library(path):
    with Store(path): pass
    return path

def test_remembers_successful_library_and_skips_picker(tmp_path):
    data=library(tmp_path/'数据目录 中文 空格')
    memory=StartupMemory(tmp_path/'程序',tmp_path/'本机设置')
    saved=memory.remember(data)
    assert saved==tmp_path/'程序'/STARTUP_FILE
    record=json.loads(saved.read_text(encoding='utf-8'))
    assert record['relative'] and 'api' not in saved.read_text(encoding='utf-8').lower()
    def never(*args): raise AssertionError('已有目录不应弹出选择窗口')
    fresh=StartupMemory(tmp_path/'程序',tmp_path/'本机设置')
    assert fresh.choose_directory(never)==str(data.resolve())
    assert fresh.choose_directory(never,'显式指定目录')=='显式指定目录'
    assert not (tmp_path/'本机设置').exists()

def test_portable_relative_locator_moves_with_program_and_library(tmp_path):
    bundle=tmp_path/'便携包'
    data=library(bundle/'文献库')
    memory=StartupMemory(bundle/'程序',tmp_path/'本机设置')
    memory.remember(data)
    moved=tmp_path/'移动后 中文 目录'; shutil.move(str(bundle),moved)
    reopened=StartupMemory(moved/'程序',tmp_path/'本机设置')
    assert reopened.last_directory()==(moved/'文献库').resolve()
    assert reopened.choose_directory(lambda *args:pytest.fail('不应重新选择'))==str((moved/'文献库').resolve())

def test_missing_or_malformed_locator_prompts_without_recreating_old_library(tmp_path):
    data=library(tmp_path/'旧目录')
    memory=StartupMemory(tmp_path/'程序',tmp_path/'本机设置'); memory.remember(data)
    shutil.move(str(data),tmp_path/'另一个位置')
    calls=[]
    assert memory.choose_directory(lambda *args:calls.append(args) or '')==''
    assert '不可用' in calls[0][1] and not data.exists()
    (tmp_path/'程序'/STARTUP_FILE).write_text('{bad json',encoding='utf-8')
    assert memory.last_directory() is None
    assert memory.choose_directory(lambda *args:'新选择目录')=='新选择目录'
    with pytest.raises(ValueError,match='成功打开'): memory.remember(tmp_path/'未建文献库')

def test_readonly_program_uses_newer_local_fallback(tmp_path,monkeypatch):
    import perovskite.startup as startup
    first=library(tmp_path/'文献库一'); second=library(tmp_path/'文献库二')
    app=tmp_path/'只读程序'; fallback=tmp_path/'本机设置'
    memory=StartupMemory(app,fallback); memory.remember(first)
    replace=startup.os.replace
    def readonly(source,target):
        if Path(target).parent==app: raise PermissionError('read only')
        return replace(source,target)
    monkeypatch.setattr(startup.os,'replace',readonly)
    assert memory.remember(second)==fallback/STARTUP_FILE
    assert StartupMemory(app,fallback).last_directory()==second.resolve()
    assert not list(app.glob('.startup-*'))
    assert not list(fallback.glob('.startup-*'))
    is_file=Path.is_file
    def inaccessible(path):
        if path==second/'library.sqlite': raise PermissionError('drive unavailable')
        return is_file(path)
    monkeypatch.setattr(Path,'is_file',inaccessible)
    assert memory.choose_directory(lambda *args:'重新选的库')=='重新选的库'

def test_failed_memory_writes_leave_library_usable(tmp_path,monkeypatch):
    import perovskite.startup as startup
    data=library(tmp_path/'可用文献库')
    memory=StartupMemory(tmp_path/'程序',tmp_path/'本机设置')
    def fail(*args): raise PermissionError('read only')
    monkeypatch.setattr(startup.os,'replace',fail)
    with pytest.raises(OSError,match='重新选择'): memory.remember(data)
    with Store(data) as store: assert store.list_articles()==[]
    assert not list(tmp_path.rglob('.startup-*'))

def test_frozen_program_locator_is_beside_exe_not_extraction(tmp_path,monkeypatch):
    import sys
    executable=tmp_path/'便携exe'/'助手.exe'
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(executable))
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path/'临时解包'),raising=False)
    memory=StartupMemory(fallback_directory=tmp_path/'本机设置')
    assert memory.app_directory==executable.parent.resolve()

def test_real_entry_switch_then_next_launch_opens_remembered_library(tmp_path):
    import subprocess
    import sys
    first=library(tmp_path/'第一个库'); second=library(tmp_path/'第二个库')
    script='''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication,QFileDialog
from PySide6.QtCore import QTimer
import perovskite.startup as startup
from perovskite.gui import Window
import app
base=Path(sys.argv[1]); mode=sys.argv[2]
first=base/'第一个库'; second=base/'第二个库'
memory=startup.StartupMemory(base/'程序',base/'设置')
startup.StartupMemory=lambda:memory
def picker(parent,*args):
    if mode=='reopen': raise AssertionError('重启不得再次要求选择目录')
    return str(first if parent is None else second)
QFileDialog.getExistingDirectory=picker
original=Window.show
def show(self):
    original(self)
    if mode=='switch' and self.store.root==first.resolve():
        QTimer.singleShot(50,self.change_data_directory)
    else:
        assert self.store.root==second.resolve()
        def finish():
            if memory.last_directory()!=second.resolve():
                QApplication.instance().exit(9); return
            self.close()
        QTimer.singleShot(100,finish)
Window.show=show
sys.argv=['app.py']
code=app.main()
assert code==0 and memory.last_directory()==second.resolve()
'''
    for mode in ['switch','reopen']:
        result=subprocess.run([sys.executable,'-c',script,str(tmp_path),mode],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
        assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
