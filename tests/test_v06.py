"""Synthetic acceptance of the v0.6 reading and editing workflows."""
import copy
import json
import time
from pathlib import Path
import pytest
from PySide6.QtCore import Qt,QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication,QLineEdit,QPlainTextEdit,QListWidget,QDialog
from PySide6.QtTest import QTest
from perovskite.store import Store,restore
from perovskite.selftest import fixtures,result_fixture
from perovskite.exchange import export_package,apply_result
from perovskite.reader import ReaderPanel
from perovskite.gui import Window
from perovskite.editing import EDIT_ROLE,save_content_edit

@pytest.fixture(scope='module')
def qt(): return QApplication.instance() or QApplication([])

def wait(reader,qt):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        qt.processEvents()
        if not reader.active_worker and not reader.pending and not reader.fit_timer.isActive(): return
        time.sleep(.01)
    raise AssertionError('连续阅读器未空闲')

def prepared(tmp_path):
    root=tmp_path/'数据 中文 空格'; source=fixtures(tmp_path/'明确标注合成文档')
    with Store(root) as store:
        main=store.import_file(source['main']); aid=main['article_id']
        store.attach_paths(aid,[source['si_pdf'],source['si_docx']])
        manifest=export_package(store,aid,tmp_path/'合成分析包.zip')
        apply_result(store,result_fixture(store,manifest))
    return root,aid,main['file_id'],source

def find_item(tree,path):
    def walk(item):
        for col in range(tree.columnCount()):
            binding=item.data(col,EDIT_ROLE)
            if binding and binding['path']==path: return item,col
        for i in range(item.childCount()):
            found=walk(item.child(i))
            if found: return found
    for i in range(tree.topLevelItemCount()):
        found=walk(tree.topLevelItem(i))
        if found: return found
    raise AssertionError('未找到编辑字段 '+str(path))

def edit_tree(tree,path,text,qt):
    item,col=find_item(tree,path); tree.scrollToItem(item); qt.processEvents()
    rect=tree.visualRect(tree.indexFromItem(item,col))
    QTest.mouseClick(tree.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,rect.center())
    QTest.mouseDClick(tree.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,rect.center())
    qt.processEvents(); QTest.qWait(25); qt.processEvents()
    editors=[e for e in tree.findChildren(QPlainTextEdit)+tree.findChildren(QLineEdit) if e.isVisible()]
    assert editors,'双击未打开直接编辑框'
    editor=editors[-1]
    if isinstance(editor,QPlainTextEdit):
        editor.setPlainText(text); QTest.keyClick(editor,Qt.Key.Key_Return,Qt.KeyboardModifier.ControlModifier)
    else: editor.setText(text); QTest.keyClick(editor,Qt.Key.Key_Return)
    qt.processEvents(); QTest.qWait(25); qt.processEvents()

def test_old_unconfirmed_files_export_without_any_confirmation(tmp_path):
    source=fixtures(tmp_path/'无需确认测试')
    with Store(tmp_path/'文献库') as store:
        main=store.import_file(source['main'])
        with store.db: store.db.execute('UPDATE links SET confirmed=0 WHERE file_id=?',(main['file_id'],))
        manifest=export_package(store,main['article_id'],tmp_path/'直接导出.zip')
        assert manifest['files'][0]['file_id']==main['file_id']
        assert '待配对确认' not in store.list_articles()[0]['state']
        assert store.file(main['file_id'])['sha256']==manifest['files'][0]['sha256']

def test_custom_tags_rename_remove_search_backup_and_restore(tmp_path):
    source=fixtures(tmp_path/'标签合成文档')
    with Store(tmp_path/'文献库') as store:
        main=store.import_file(source['main']); aid=main['article_id']
        metadata=store.article(aid)['metadata']; metadata['tags']=['自定义 初始标签','将删除']
        store.save_metadata(aid,metadata,'')
        store.manage_tags(['修改后标签','未应用的新标签'],{'自定义 初始标签':'修改后标签'},['将删除'])
        assert store.article(aid)['metadata']['tags']==['修改后标签']
        assert store.list_articles('修改后标签')
        assert '将删除' not in store.tag_names()
        archive=tmp_path/'标签备份.zip'; store.backup(archive)
    restore(archive,tmp_path/'恢复库')
    with Store(tmp_path/'恢复库') as store:
        assert store.tag_names()==['修改后标签','未应用的新标签']
        assert store.article(aid)['metadata']['tags']==['修改后标签']

def test_continuous_pages_full_preload_scroll_arrows_zoom_cache_and_search(tmp_path,qt):
    import fitz
    source=tmp_path/'SYNTHETIC continuous 18 pages.pdf'
    with fitz.open() as doc:
        for index in range(18): doc.new_page().insert_text((40,60),f'SYNTHETIC TEST PAGE {index+1} - unique target {index+1}')
        doc.save(source)
    with Store(tmp_path/'连续页库') as store:
        result=store.import_file(source); panel=ReaderPanel(store); panel.resize(960,820); panel.show(); panel.open_file(result['file_id']); wait(panel,qt)
        view=panel.pdf_view
        assert len(view.pages)==18 and view.document.height()>view.scroll.viewport().height()*10
        assert view.render_count==18 and view.full_ready
        assert all(p.data and not p._image.isNull() for p in view.pages)
        assert view.document.width()>=panel.canvas.width()
        panel.page.stepBy(-1); wait(panel,qt)
        assert panel.page.value()==2 and view.current==1
        panel.page.stepBy(1); wait(panel,qt); assert panel.page.value()==1
        before=view.render_count; width=panel.canvas.width()
        panel.zoom.setValue(75)
        assert panel.canvas.width()!=width and view.render_count==before
        wait(panel,qt)
        panel.zoom.setValue(50); wait(panel,qt)
        assert view.render_count==before,'缩小已缓存页面不应重新生成PDF图片'
        view.scroll.verticalScrollBar().setValue(view.pages[17].y()); wait(panel,qt)
        assert panel.page.value()>=17 and view.pages[17].data
        assert view.render_count==before,'全文预加载后跳页和缩放不能重新渲染'
        assert view.cache_bytes<=view.cache_limit
        panel.query.setText('unique target 12'); panel.find_sentence(); wait(panel,qt)
        assert panel.canvas.data['page_index']==11 and panel.canvas.selected_payload()['anchors']
        panel.stop(); wait(panel,qt); panel.close()

def test_single_letter_highlight_inline_comment_delete_and_input_safety(tmp_path,qt):
    source=fixtures(tmp_path/'字符选择样本')
    with Store(tmp_path/'字母库') as store:
        main=store.import_file(source['main']); panel=ReaderPanel(store); panel.resize(960,820); panel.show(); panel.open_file(main['file_id']); wait(panel,qt)
        char=next(c for c in panel.canvas.data['characters'] if c['text']=='C')
        position=panel.canvas.screen_rect(char['display_rect']).center().toPoint()
        QTest.mouseClick(panel.canvas,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position)
        assert panel.canvas.selected_payload()['text']=='C'
        panel.save_selection(); assert store.highlights(main['file_id'])[0]['text']=='C'
        panel.marks_button.click(); item=panel.marks.topLevelItem(0)
        panel.marks.editItem(item,2); qt.processEvents()
        editor=next(e for e in panel.marks.findChildren(QPlainTextEdit) if e.isVisible())
        editor.setPlainText('直接双击编辑的中文批注'); QTest.keyClick(editor,Qt.Key.Key_Return,Qt.KeyboardModifier.ControlModifier); qt.processEvents()
        assert store.highlights(main['file_id'])[0]['note']=='直接双击编辑的中文批注'
        panel.query.setText('abcd'); panel.query.setCursorPosition(1); QTest.keyClick(panel.query,Qt.Key.Key_Delete)
        assert panel.query.text()=='acd' and len(store.highlights(main['file_id']))==1
        panel.select_mark(item.data(0,Qt.ItemDataRole.UserRole)['mark_id']); panel.canvas.setFocus(); QTest.keyClick(panel.canvas,Qt.Key.Key_Delete)
        assert store.highlights(main['file_id'])==[]
        panel.stop(); wait(panel,qt); panel.close()

def test_inline_metadata_story_process_performance_and_restart(tmp_path,qt):
    root,aid,fid,source=prepared(tmp_path)
    window=Window(root); window.resize(1280,720); window.show(); wait(window.reader,qt)
    rows=window.table.rowCount()
    for row in range(rows):
        if window.table.item(row,0).data(Qt.ItemDataRole.UserRole)==aid: window.table.selectRow(row); break
    errors=[]; window.error=lambda text:errors.append(str(text)); qt.processEvents()
    # The metadata area has ordinary fields, no large title or article-ID row.
    assert window.meta.columnCount()==2 and all(window.meta.item(i,0).text()!='文章ID' for i in range(window.meta.rowCount()))
    row=next(i for i in range(window.meta.rowCount()) if window.meta.item(i,0).text()=='通讯作者（数组）')
    cell=window.meta.item(row,1); window.meta.scrollToItem(cell); qt.processEvents()
    QTest.mouseClick(window.meta.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,window.meta.visualItemRect(cell).center())
    QTest.mouseDClick(window.meta.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,window.meta.visualItemRect(cell).center()); qt.processEvents()
    editor=next(e for e in window.meta.findChildren(QLineEdit) if e.isVisible()); editor.setText('研究者甲；Researcher B'); QTest.keyClick(editor,Qt.Key.Key_Return); qt.processEvents(); QTest.qWait(25); qt.processEvents()
    assert window.store.article(aid)['metadata']['corresponding_authors']==['研究者甲','Researcher B']
    window.list_details.click(); assert not window.table.isColumnHidden(4)
    current=next(i for i in range(window.table.rowCount()) if window.table.item(i,0).data(Qt.ItemDataRole.UserRole)==aid)
    assert '研究者甲' in window.table.item(current,4).text()
    window.tabs.setCurrentWidget(window.story_tab); edit_tree(window.story,['one_sentence',0,'text'],'人工直接编辑故事，明确为合成测试。',qt)
    assert window.store.section(aid,'story')['content']['one_sentence'][0]['text']=='人工直接编辑故事，明确为合成测试。'
    window.tabs.setCurrentWidget(window.process_tab); edit_tree(window.process,['samples',0,'steps',0,'parameters',0,'original_value'],'105',qt)
    content=window.store.section(aid,'process')['content']; assert content['samples'][0]['steps'][0]['parameters'][0]['original_value']==105
    assert content['samples'][1]['steps'][0]['parameters'][0]['original_value']==110
    window.tabs.setCurrentWidget(window.performance_tab); edit_tree(window.performance,['performance',0,'values',0,'original_value'],'21.5',qt)
    assert window.store.section(aid,'process')['content']['performance'][0]['values'][0]['original_value']==21.5
    assert not errors
    window.reader.stop(); wait(window.reader,qt); window.close(); qt.processEvents()
    with Store(root) as reopened:
        assert reopened.section(aid,'process')['content']['performance'][0]['values'][0]['original_value']==21.5
        assert reopened.rows("SELECT * FROM history WHERE article_id=? AND source='双击区域人工编辑'",(aid,))

def test_tabs_preserved_all_sections_and_toolbar_accessibility(tmp_path,qt):
    root,aid,fid,source=prepared(tmp_path)
    window=Window(root); window.resize(1280,720); window.show(); wait(window.reader,qt)
    for tab in [window.meta_tab,window.story_tab,window.process_tab,window.performance_tab,window.viewer_tab]:
        window.tabs.setCurrentWidget(tab)
        for row in [0,window.table.rowCount()-1]:
            window.table.selectRow(row); qt.processEvents(); assert window.tabs.currentWidget() is tab
    labels=[action.toolTip() for action in window.toolbar.actions()]
    assert {'完整备份','恢复到空目录','切换数据目录','自定义标签'}<=set(labels)
    assert all(window.toolbar.widgetForAction(a).accessibleName() for a in window.toolbar.actions())
    window.reader.stop(); wait(window.reader,qt); window.close(); qt.processEvents()

def test_raw_value_edit_clears_stale_normalization_and_preserves_history(tmp_path):
    root,aid,fid,source=prepared(tmp_path)
    with Store(root) as store:
        section=store.section(aid,'process'); content=copy.deepcopy(section['content'])
        param=content['samples'][0]['steps'][0]['parameters'][0]
        param.update(normalized_value=373.15,normalized_unit='K',conversion_note='100 C + 273.15')
        with store.db: store.save_section(aid,'process',content,section['evidence'],section['checks'],'测试已核验',True)
        save_content_edit(store,aid,'process',['samples',0,'steps',0,'parameters',0,'original_value'],'105','scalar')
        updated=store.section(aid,'process'); param=updated['content']['samples'][0]['steps'][0]['parameters'][0]
        assert param['original_value']==105 and param['normalized_value'] is None and not updated['human_verified']

def test_tag_dialog_can_add_rename_and_apply_without_json(tmp_path,qt,monkeypatch):
    from PySide6.QtWidgets import QInputDialog,QPushButton
    root,aid,fid,source=prepared(tmp_path); window=Window(root)
    for row in range(window.table.rowCount()):
        if window.table.item(row,0).data(Qt.ItemDataRole.UserRole)==aid: window.table.selectRow(row); break
    monkeypatch.setattr(QInputDialog,'getText',lambda *args:('用户自建标签',True))
    def manage(dialog):
        items=dialog.findChild(QListWidget)
        items.item(0).setText('用户改名标签'); items.item(0).setCheckState(Qt.CheckState.Checked)
        next(b for b in dialog.findChildren(QPushButton) if b.text()=='新增').click()
        items.item(items.count()-1).setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog,'exec',manage)
    window.manage_tags()
    assert {'用户改名标签','用户自建标签'}<=set(window.store.tag_names())
    assert window.store.article(aid)['metadata']['tags']==['用户改名标签','用户自建标签']
    window.reader.stop(); wait(window.reader,qt); window.close(); qt.processEvents()

def test_cross_page_character_selection_has_both_page_anchors(tmp_path,qt):
    import fitz
    path=tmp_path/'SYNTHETIC 两页选择.pdf'
    with fitz.open() as doc:
        for text in ['ABCDE','FGHIJ']: doc.new_page().insert_text((40,60),text)
        doc.save(path)
    with Store(tmp_path/'跨页库') as store:
        imported=store.import_file(path); reader=ReaderPanel(store); reader.resize(960,850); reader.show(); reader.open_file(imported['file_id']); wait(reader,qt)
        first,second=reader.pdf_view.pages
        point=second.mapToGlobal(second.screen_rect(second.data['characters'][1]['display_rect']).center().toPoint())
        reader.pdf_view.drag_across(first,2,point)
        payload=reader.selection_payload()
        assert payload['text']=='CDE\nFG'
        assert {a['page_index'] for a in payload['anchors']}=={0,1}
        reader.save_selection(); assert len(store.highlights(imported['file_id'])[0]['anchors'])==2
        reader.stop(); wait(reader,qt); reader.close()
