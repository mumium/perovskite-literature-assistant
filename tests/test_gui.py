import pytest
from PySide6.QtWidgets import QApplication
from perovskite.gui import Window

def wait_reader(window,app):
    import time
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        app.processEvents()
        reader=window.reader
        if not reader.active_worker and not reader.pending and not reader.fit_timer.isActive(): return
        time.sleep(.01)
    raise AssertionError('原文自动加载超时')

def article_row(window,aid):
    from PySide6.QtCore import Qt
    return next(i for i in range(window.table.rowCount()) if window.table.item(i,0).data(Qt.ItemDataRole.UserRole)==aid)

def test_data_directory_lock_and_reopen(tmp_path):
    app = QApplication.instance() or QApplication([])
    first = Window(tmp_path / "锁测试 中文 目录")
    with pytest.raises(ValueError, match="正在使用"):
        Window(tmp_path / "锁测试 中文 目录")
    first.close()
    app.processEvents()
    second = Window(tmp_path / "锁测试 中文 目录")
    assert second.table.rowCount() == 0
    second.reader.focus_button.click()
    assert second._reading_mode and second.center_panel.isHidden()
    second.reader.focus_button.click()
    assert not second._reading_mode and not second.center_panel.isHidden()
    second.close()

def test_small_window_auto_read_switch_and_clear(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from perovskite.selftest import fixtures
    from perovskite.store import Store
    app=QApplication.instance() or QApplication([])
    app.setFont(QFont('Microsoft YaHei UI',10))
    files=fixtures(tmp_path/'明确标注合成文档')
    root=tmp_path/'小屏幕 文献库'
    with Store(root) as store:
        si=store.import_file(files['si_docx'])
        main=store.import_file(files['main'])
        other=store.import_file(files['scanned'])
        store.associate(si['file_id'],main['article_id'],'si')
        empty=store.new_article({'title':'没有附件的测试文章'})
        store.save_metadata(empty,store.article(empty)['metadata'],'测试文章只有人工笔记')
        for i in range(18):
            aid=store.new_article({'title':f'测试长标题 {i} '+ 'long title '*20})
            store.save_metadata(aid,store.article(aid)['metadata'],'明确标注的布局测试记录')
    window=Window(root); errors=[]; window.reader.error.connect(errors.append)
    window.resize(1280,720); window.show(); app.processEvents()
    window.table.selectRow(article_row(window,main['article_id'])); wait_reader(window,app)
    assert window.reader.file_id==main['file_id']
    assert window.tabs.currentWidget() is window.meta_tab
    assert window.reader.canvas.data['file_id']==main['file_id']
    assert window.reader.file_picker.count()==2
    window.reader.file_picker.setCurrentIndex(window.reader.file_picker.findData(si['file_id']))
    assert window.reader.file_id==si['file_id'] and window.reader.kind=='docx'
    # Clicking a document preserves the selected tab and attachment.
    window.tabs.setCurrentWidget(window.story_tab)
    row=article_row(window,main['article_id'])
    window.table.scrollToItem(window.table.item(row,0)); app.processEvents()
    QTest.mouseClick(window.table.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,window.table.visualItemRect(window.table.item(row,0)).center())
    wait_reader(window,app)
    assert window.reader.file_id==si['file_id'] and window.tabs.currentWidget() is window.story_tab
    window.reader.file_picker.setCurrentIndex(window.reader.file_picker.findData(main['file_id'])); wait_reader(window,app)
    assert window.windowTitle()=='钙钛矿文献助手 · MK Design' and window.banner.text()=='钙钛矿文献助手' and window.signature.text()=='MK Design'
    assert min(1200,app.primaryScreen().availableGeometry().width()-32)<=window.width()<=1280 and window.height()==720
    assert window.reader.scroll.viewport().height()>=400
    assert window.table.viewport().height()//window.table.rowHeight(0)>=15
    assert window.table.rowHeight(0)==32
    assert window.table.isColumnHidden(1) and window.table.columnWidth(0)>=260
    window.list_details.click(); assert not window.table.isColumnHidden(3) and not window.table.isColumnHidden(4)
    window.list_details.click()
    assert window.reader.mark_details.isHidden() and window.reader.tools.isHidden()
    window.reader.tools_button.click(); assert not window.reader.tools.isHidden()
    window.reader.tools_button.click()
    window.reader.marks_button.click(); assert not window.reader.mark_details.isHidden()
    window.reader.marks_button.click()
    # Rapid switching must never paint the previous article after cancellation.
    window.table.selectRow(article_row(window,other['article_id']))
    window.table.selectRow(article_row(window,main['article_id'])); wait_reader(window,app)
    assert window.reader.canvas.data['file_id']==main['file_id']
    window.table.selectRow(article_row(window,empty)); wait_reader(window,app)
    assert window.reader.file_id is None and window.reader.canvas.data is None
    assert '没有附件' in window.reader.status.text()
    assert not errors
    window.close(); app.processEvents()

def test_associate_uses_disk_file_picker_without_article_list(tmp_path,monkeypatch):
    import time
    from PySide6.QtWidgets import QFileDialog,QDialog,QComboBox
    from perovskite.store import Store
    from perovskite.selftest import fixtures
    app=QApplication.instance() or QApplication([])
    source=fixtures(tmp_path/'电脑上选择的 合成文档')
    root=tmp_path/'附件选择 数据目录'
    with Store(root) as store: main=store.import_file(source['main'])
    window=Window(root); window.resize(1280,720); window.show(); wait_reader(window,app)
    window.table.selectRow(article_row(window,main['article_id']))
    assert window.attachments.currentItem() is None
    pick_calls=[]
    def pick(*args):
        pick_calls.append(args)
        return [str(source['si_pdf']),str(source['si_docx'])],'文献附件'
    monkeypatch.setattr(QFileDialog,'getOpenFileNames',pick)
    def role_dialog(dialog):
        assert dialog.windowTitle()=='关联到当前文献'
        choices=dialog.findChildren(QComboBox)
        assert len(choices)==1 and choices[0].currentData()=='si'
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog,'exec',role_dialog)
    window.pair_file()
    deadline=time.monotonic()+10
    while window.worker and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.01)
    assert window.worker is None and pick_calls
    wait_reader(window,app)
    assert len(window.store.files(main['article_id']))==3
    assert window.article_id==main['article_id']
    assert window.tabs.currentWidget() is window.attach_tab
    assert '已关联 2 个附件' in window.status.text()
    monkeypatch.setattr(QFileDialog,'getOpenFileNames',lambda *args:([],''))
    window.pair_file()
    assert window.worker is None and len(window.store.files(main['article_id']))==3
    monkeypatch.setattr(QFileDialog,'getOpenFileNames',lambda *args:([str(source['scanned'])],''))
    monkeypatch.setattr(QDialog,'exec',lambda dialog:QDialog.DialogCode.Rejected)
    window.pair_file()
    assert window.worker is None and len(window.store.files(main['article_id']))==3
    requested=[]; window.directoryRequested.connect(requested.append)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(tmp_path/'另一个文献库'))
    window.change_data_directory()
    from perovskite.startup import DATA_FOLDER
    assert requested==[str(tmp_path/'另一个文献库'/DATA_FOLDER)]
    assert window.store.root==root.resolve()
    window.close(); app.processEvents()
