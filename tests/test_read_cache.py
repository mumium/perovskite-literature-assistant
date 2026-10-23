"""Real hover feedback and retained document switching using synthetic files."""
import time
from collections import Counter
import fitz
import pytest
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QTabWidget
from perovskite.reader import ReaderPanel
from perovskite.gui import Window
from perovskite.store import Store
from perovskite.selftest import fixtures
from perovskite.theme import apply_theme
from test_v07 import wait


@pytest.fixture(scope='module')
def app(): return QApplication.instance() or QApplication([])


def paper(path,count,label):
    with fitz.open() as doc:
        for index in range(count):
            page=doc.new_page()
            for row in range(18): page.insert_text((40,60+row*25),f'SYNTHETIC TEST ONLY {label} page {index+1} row {row+1}')
        doc.save(path)
    return path


def finish(panel,app):
    panel.stop(); wait(panel,app); panel.close(); app.processEvents()


def test_toolbar_real_hover_pressed_feedback_and_text_tabs(tmp_path,app):
    window=Window(tmp_path/'empty library'); window.show(); window.activateWindow(); window.raise_(); QTest.qWait(100)
    try:
        assert isinstance(window.tabs,QTabWidget)
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())]==['文献信息','核心故事','工艺','性能与稳定性','证据核验','附件与配对','原文查看']
        assert all(window.tabs.tabIcon(i).isNull() for i in range(window.tabs.count()))
        button=window.toolbar.widgetForAction(window.toolbar.actions()[0])
        def pixel():
            image=button.grab().toImage(); ratio=image.devicePixelRatio()
            return image.pixelColor(round(3*ratio),round(button.height()/2*ratio)).name()
        for mode in ['light','dark']:
            apply_theme(mode=mode); QTest.qWait(100)
            QTest.mouseMove(window.banner,QPoint(10,10)); button.clearFocus(); QTest.qWait(100)
            normal=pixel()
            QTest.mouseMove(button,button.rect().center()); QTest.qWait(100)
            hover=pixel()
            button.setDown(True); app.processEvents(); pressed=pixel(); button.setDown(False)
            assert len({normal,hover,pressed})==3,'无框图标必须有可见的悬停和按下反馈'
            assert button.toolTip() and button.iconSize().width()==22
    finally:
        window.close(); apply_theme(mode='light'); app.processEvents()


def test_completed_pdfs_switch_synchronously_without_open_hash_extract_or_preload(tmp_path,app,monkeypatch):
    with Store(tmp_path/'library') as store:
        a=store.import_file(paper(tmp_path/'A.pdf',3,'A'))
        b=store.import_file(paper(tmp_path/'B.pdf',2,'B'))
        panel=ReaderPanel(store); panel.resize(960,750); panel.show()
        try:
            panel.open_file(a['file_id']); wait(panel,app)
            panel.zoom.setValue(180); panel.pdf_view.navigate(1); wait(panel,app)
            view=panel.pdf_view; view.scroll.verticalScrollBar().setValue(view.scroll.verticalScrollBar().value()+47); wait(panel,app)
            view.scroll.horizontalScrollBar().setValue(90)
            position=(view.scroll.horizontalScrollBar().value(),view.scroll.verticalScrollBar().value())
            adoc=view.document; aimage=view.pages[1]._image.cacheKey(); commands=view.pages[1].data['display_list']
            panel.canvas.select_chars(0,8); panel.note.setText('缓存切换批注'); panel.save_selection()
            panel.open_file(b['file_id']); wait(panel,app); bdoc=view.document
            calls=[]
            with monkeypatch.context() as patch:
                def forbidden(*a,**k): pytest.fail('已完成PDF的切换不得重新打开、校验整文件或预载')
                patch.setattr(fitz,'open',forbidden)
                patch.setattr('perovskite.continuous.digest',forbidden)
                patch.setattr('perovskite.reader.load_pdf_page',forbidden)
                for _ in range(4):
                    start=time.perf_counter(); panel.open_file(a['file_id']); calls.append(time.perf_counter()-start)
                    assert view.document is adoc and view.full_ready and view.render_count==3
                    assert view.pages[1]._image.cacheKey()==aimage and view.pages[1].data['display_list'] is commands
                    assert (view.scroll.horizontalScrollBar().value(),view.scroll.verticalScrollBar().value())==position
                    assert panel.marks.topLevelItemCount()==1 and panel.zoom.value()==180
                    assert all(page.data['file_id']==a['file_id'] for page in view.pages)
                    start=time.perf_counter(); panel.open_file(b['file_id']); calls.append(time.perf_counter()-start)
                    assert view.document is bdoc and view.full_ready and view.render_count==2
                    assert not view.marks and not panel.marks.topLevelItemCount()
                    app.processEvents()
            assert max(calls)<1.0
            print('Cached switches (ms):',','.join(str(round(t*1000,1)) for t in calls))
            assert view.session_hits>=8
        finally: finish(panel,app)
        assert not view.sessions and not view.cache


def test_partial_pdf_only_resumes_missing_pages(tmp_path,app,monkeypatch):
    import perovskite.reader as reader
    original=reader.load_pdf_page; calls=Counter()
    def slowed(path,index,*args,**kwargs):
        calls[(path.name,index)]+=1; time.sleep(.025)
        return original(path,index,*args,**kwargs)
    monkeypatch.setattr(reader,'load_pdf_page',slowed)
    with Store(tmp_path/'partial library') as store:
        a=store.import_file(paper(tmp_path/'partial.pdf',14,'PARTIAL'))
        b=store.import_file(paper(tmp_path/'other.pdf',1,'OTHER'))
        panel=ReaderPanel(store); panel.show()
        try:
            panel.open_file(a['file_id']); deadline=time.monotonic()+15
            while len(panel.pdf_view.cache)<2 and time.monotonic()<deadline: app.processEvents(); time.sleep(.005)
            view=panel.pdf_view; assert 2<=len(view.cache)<14
            completed={key[1]:data['image'].cacheKey() for key,(data,_) in view.cache.items()}
            counts={index:calls[(store.path(a['file_id']).name,index)] for index in completed}
            panel.open_file(b['file_id']); wait(panel,app)
            panel.open_file(a['file_id']); wait(panel,app)
            assert view.full_ready and len(view.cache)==14
            assert all(view.cached(i,100)['image'].cacheKey()==key for i,key in completed.items())
            assert all(calls[(store.path(a['file_id']).name,i)]==count for i,count in counts.items())
        finally: finish(panel,app)


def test_cached_fit_reading_position_survives_window_width_changes(tmp_path,app,monkeypatch):
    with Store(tmp_path/'resize library') as store:
        a=store.import_file(paper(tmp_path/'resize A.pdf',4,'RESIZE A'))
        b=store.import_file(paper(tmp_path/'resize B.pdf',2,'RESIZE B'))
        panel=ReaderPanel(store); panel.resize(850,740); panel.show()
        try:
            panel.open_file(a['file_id']); wait(panel,app); view=panel.pdf_view
            view.navigate(2); view.scroll.verticalScrollBar().setValue(view.scroll.verticalScrollBar().value()+90); wait(panel,app)
            index=view.current; page=view.pages[index]
            fraction=(view.scroll.verticalScrollBar().value()-page.y())/page.height()
            document=view.document
            for width in [1200,700]:
                panel.open_file(b['file_id']); wait(panel,app)
                panel.resize(width,740); wait(panel,app)
                with monkeypatch.context() as patch:
                    patch.setattr('perovskite.reader.load_pdf_page',lambda *a,**k:pytest.fail('改变窗口宽度不应重复预载缓存PDF'))
                    panel.open_file(a['file_id']); wait(panel,app)
                assert view.document is document and view.full_ready and view.current==index
                page=view.pages[index]
                restored=(view.scroll.verticalScrollBar().value()-page.y())/page.height()
                assert restored==pytest.approx(fraction,abs=.005)
        finally: finish(panel,app)


@pytest.mark.parametrize('bound',['count','memory'])
def test_cached_changed_pdf_rejected_and_lru_releases_old_documents(tmp_path,app,bound):
    with Store(tmp_path/'bounded library') as store:
        files=[store.import_file(paper(tmp_path/f'{label}.pdf',1,label)) for label in ['A','B','C']]
        panel=ReaderPanel(store); panel.show(); view=panel.pdf_view; view.session_max_documents=2 if bound=='count' else 100; errors=[]; panel.error.connect(errors.append)
        try:
            for file in files[:2]:
                panel.open_file(file['file_id']); wait(panel,app)
                if bound=='memory': view.session_limit=2*view.cache_bytes+1000
            panel.open_file(files[0]['file_id']); wait(panel,app)
            panel.open_file(files[2]['file_id']); wait(panel,app)
            assert {key[1] for key in view.sessions}=={files[0]['file_id']}
            assert '释放' in view.cache_notice
            with fitz.open(store.path(files[0]['file_id'])) as changed:
                changed[0].insert_text((40,700),'SYNTHETIC external change'); changed.saveIncr()
            panel.open_file(files[0]['file_id']); wait(panel,app)
            assert not view.full_ready and not view.pages and errors and '哈希' in errors[-1]
            assert all(key[1]!=files[0]['file_id'] for key in view.sessions)
        finally: finish(panel,app)


def test_docx_document_and_pdf_cache_survive_other_file_and_empty_article(tmp_path,app,monkeypatch):
    with Store(tmp_path/'mixed library') as store:
        source=fixtures(tmp_path/'synthetic files')
        pdf=store.import_file(source['main']); docx=store.import_file(source['si_docx'])
        panel=ReaderPanel(store); panel.show()
        try:
            panel.open_file(pdf['file_id']); wait(panel,app); original=panel.pdf_view.document
            panel.open_file(docx['file_id']); document=panel.doc_text.document(); spans=panel.spans
            cursor=document.find('Control sample'); panel.doc_text.setTextCursor(cursor); panel.note.setText('DOCX缓存批注'); panel.save_selection()
            panel.zoom.setValue(150)
            panel.open_file(pdf['file_id']); wait(panel,app)
            assert panel.pdf_view.document is original
            monkeypatch.setattr('perovskite.reader.docx_display',lambda *a:pytest.fail('缓存DOCX不应重新生成文本'))
            for _ in range(3):
                panel.open_file(docx['file_id'])
                assert panel.doc_text.document() is document and panel.spans is spans and panel.zoom.value()==150
                assert panel.doc_text.extraSelections() and panel.marks.topLevelItemCount()==1
                panel.clear_file(); panel.open_file(pdf['file_id']); wait(panel,app)
                assert panel.pdf_view.document is original and panel.pdf_view.full_ready
            assert panel.text_session_hits==3
        finally: finish(panel,app)
        assert not panel.text_sessions and not panel.pdf_view.sessions
