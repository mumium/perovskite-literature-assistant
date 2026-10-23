"""Fixed-image PDF mode, vector-detail restoration and persisted preferences."""
import json
import threading
import time
import fitz
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from perovskite.reader import ReaderPanel
from perovskite.store import Store,atomic_json
from perovskite.selftest import fixtures
from perovskite.theme import apply_theme
from test_read_cache import paper,finish
from test_v07 import wait


@pytest.fixture(scope='module')
def app(): return QApplication.instance() or QApplication([])


def click(panel): QTest.mouseClick(panel.browse_button,Qt.MouseButton.LeftButton)


def test_switch_fast_disables_detail_then_restores_hd_without_reload(tmp_path,app,monkeypatch):
    with Store(tmp_path/'library') as store:
        item=store.import_file(paper(tmp_path/'synthetic.pdf',2,'BROWSE MODES'))
        panel=ReaderPanel(store); panel.resize(1000,750); panel.show()
        try:
            panel.open_file(item['file_id']); panel.zoom.setValue(400); wait(panel,app)
            view=panel.pdf_view; assert panel.canvas.hd and not view.fast_browse
            image=panel.canvas._image.cacheKey(); commands=panel.canvas.data['display_list']; count=view.render_count
            click(panel)
            assert panel.fast_browse and view.fast_browse and panel.browse_button.text()=='高清浏览'
            assert all(page.hd is None for page in view.pages)
            assert json.loads((store.root/'config.json').read_text(encoding='utf-8'))['pdf_browse_mode']=='fast'
            hd_count=view.hd_render_count
            with monkeypatch.context() as patch:
                patch.setattr('perovskite.continuous.render_detail',lambda *a:pytest.fail('加速模式不能补绘高清'))
                panel.zoom.setValue(350); view.request_visible(); wait(panel,app)
                assert view.hd_render_count==hd_count and all(page.hd is None for page in view.pages)
                panel.canvas.select_chars(0,8); panel.note.setText('加速模式中的高亮批注'); panel.save_selection()
                assert len(store.highlights(item['file_id']))==1
            with monkeypatch.context() as patch:
                def forbidden(*a,**k): pytest.fail('模式切换不能重新读取PDF或重做全部页面')
                patch.setattr(fitz,'open',forbidden); patch.setattr('perovskite.reader.load_pdf_page',forbidden)
                click(panel); wait(panel,app)
            assert not view.fast_browse and panel.canvas.hd and panel.browse_button.text()=='加速浏览'
            assert view.render_count==count and panel.canvas._image.cacheKey()==image and panel.canvas.data['display_list'] is commands
            assert panel.zoom.value()==350 and panel.marks.topLevelItemCount()==1
        finally: finish(panel,app)


def test_remember_mode_reopen_and_cold_fast_load_at_fixed_quality(tmp_path,app,monkeypatch):
    with Store(tmp_path/'portable library') as store:
        item=store.import_file(paper(tmp_path/'fixed.pdf',2,'FIXED RESOLUTION'))
        store.save_reader_state(item['file_id'],0,400,'manual')
        cfg=json.loads((store.root/'config.json').read_text(encoding='utf-8')); cfg.update(theme='dark',user_tags=['自定义标签'])
        atomic_json(store.root/'config.json',cfg)
        first=ReaderPanel(store); first.show(); click(first); finish(first,app)
        panel=ReaderPanel(store); panel.resize(1000,750); panel.show()
        try:
            assert panel.fast_browse and panel.browse_button.isChecked() and panel.browse_button.text()=='高清浏览'
            with monkeypatch.context() as patch:
                patch.setattr('perovskite.continuous.render_detail',lambda *a:pytest.fail('重启恢复的加速模式不能绘制高清'))
                panel.open_file(item['file_id']); wait(panel,app)
                view=panel.pdf_view; assert view.quality==100 and view.full_ready and panel.zoom.value()==400
                assert all(page.hd is None for page in view.pages) and view.hd_render_count==0
                image=panel.canvas._image.cacheKey(); panel.zoom.setValue(150); wait(panel,app)
                assert panel.canvas._image.cacheKey()==image
            saved=json.loads((store.root/'config.json').read_text(encoding='utf-8'))
            assert saved['theme']=='dark' and saved['user_tags']==['自定义标签'] and saved['api_enabled'] is False
            click(panel); wait(panel,app); finish(panel,app)
            panel=ReaderPanel(store); panel.show()
            assert not panel.fast_browse and panel.browse_button.text()=='加速浏览'
            errors=[]; panel.error.connect(errors.append)
            monkeypatch.setattr('perovskite.reader.atomic_json',lambda *a:(_ for _ in ()).throw(OSError('read-only test')))
            click(panel)
            assert errors and not panel.fast_browse and not panel.browse_button.isChecked() and not panel.pdf_view.fast_browse
        finally: finish(panel,app)


def test_fast_mode_covers_cached_documents_and_button_geometry_in_both_themes(tmp_path,app):
    with Store(tmp_path/'mixed library') as store:
        files=fixtures(tmp_path/'synthetic documents')
        a=store.import_file(files['main']); b=store.import_file(paper(tmp_path/'other.pdf',1,'OTHER')); text=store.import_file(files['si_docx'])
        panel=ReaderPanel(store); panel.resize(1000,750); panel.show()
        try:
            panel.open_file(a['file_id']); panel.zoom.setValue(400); wait(panel,app); assert panel.canvas.hd
            document=panel.pdf_view.document
            panel.open_file(b['file_id']); wait(panel,app); click(panel)
            panel.open_file(a['file_id']); wait(panel,app)
            assert panel.pdf_view.document is document and panel.pdf_view.fast_browse and not panel.canvas.hd
            panel.open_file(text['file_id']); assert panel.fast_browse and panel.doc_text.toPlainText()
            click(panel); assert not panel.fast_browse
            panel.open_file(a['file_id']); wait(panel,app); assert panel.canvas.hd
            for mode in ['light','dark']:
                apply_theme(mode=mode); app.processEvents()
                assert panel.browse_button.size()==panel.focus_button.size()
                buttons=[panel.focus_button,panel.browse_button,panel.tools_button]
                assert buttons[0].geometry().right()<buttons[1].geometry().left()<buttons[2].geometry().left()
                assert len({button.geometry().center().y() for button in buttons})==1
                image=panel.browse_button.grab().toImage(); d=image.devicePixelRatio()
                left=image.pixelColor(round(5*d),round(panel.browse_button.height()/2*d))
                right=image.pixelColor(round((panel.browse_button.width()-6)*d),round(panel.browse_button.height()/2*d))
                assert left!=right and max(left.red(),left.green(),left.blue())-min(left.red(),left.green(),left.blue())>50
        finally: finish(panel,app); apply_theme(mode='light')


def test_enter_fast_while_detail_running_discards_late_result(tmp_path,app,monkeypatch):
    import perovskite.continuous as continuous
    original=continuous.render_detail; started=threading.Event(); released=threading.Event()
    def slow(display_list,rect,scale,cancel):
        started.set()
        while not released.is_set() and not cancel(): time.sleep(.005)
        return original(display_list,rect,scale,cancel)
    with Store(tmp_path/'cancel library') as store:
        item=store.import_file(paper(tmp_path/'cancel.pdf',1,'CANCEL DETAIL'))
        panel=ReaderPanel(store); panel.show()
        try:
            panel.open_file(item['file_id']); wait(panel,app)
            with monkeypatch.context() as patch:
                patch.setattr(continuous,'render_detail',slow); panel.zoom.setValue(400)
                deadline=time.monotonic()+10
                while not started.is_set() and time.monotonic()<deadline: app.processEvents(); time.sleep(.005)
                assert started.is_set(); click(panel); released.set(); wait(panel,app)
                assert panel.pdf_view.fast_browse and not panel.canvas.hd and panel.pdf_view.load_error is None
            click(panel); wait(panel,app); assert panel.canvas.hd
        finally: released.set(); finish(panel,app)
