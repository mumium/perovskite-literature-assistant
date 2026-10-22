"""Synthetic vector-detail, reading controls, page comments and new-library tests."""
import copy
import json
import sqlite3
import time
from contextlib import closing
import fitz
import pytest
from PySide6.QtCore import Qt,QPoint,QPointF
from PySide6.QtGui import QWheelEvent,QColor,QFont
from PySide6.QtWidgets import QApplication,QStyleOptionSpinBox,QStyle,QPushButton
from PySide6.QtTest import QTest
from perovskite.reader import ReaderPanel,slider_zoom,zoom_slider
from perovskite.store import Store,restore,DB_VERSION
from perovskite.startup import StartupMemory,library_in_folder,DATA_FOLDER
from perovskite.selftest import fixtures
from perovskite.pdf_annotations import save_annotated_pdf
from perovskite.extract import digest

@pytest.fixture(scope='module')
def qt(): return QApplication.instance() or QApplication([])

def wait(panel,qt):
    end=time.monotonic()+20
    while time.monotonic()<end:
        qt.processEvents()
        if not panel.active_worker and not panel.pending and not panel.fit_timer.isActive(): return
        time.sleep(.005)
    raise AssertionError('阅读器后台任务超时')

def prepared(tmp_path,qt,rotation=0):
    source=fixtures(tmp_path/'明确标注的合成文档')
    if rotation:
        with fitz.open(source['main']) as doc: doc[0].set_rotation(rotation); doc.saveIncr()
    store=Store(tmp_path/'数据 中文 空格'); main=store.import_file(source['main'])
    panel=ReaderPanel(store); panel.resize(920,760); panel.show(); panel.open_file(main['file_id']); wait(panel,qt)
    return store,main,panel,source

def cleanup(store,panel,qt):
    panel.stop(); wait(panel,qt); panel.pdf_view.clear(); panel.close(); qt.processEvents(); store.close()

@pytest.mark.parametrize('rotation',[0,90])
def test_vector_commands_retained_and_zoom_detail_never_reopens_pdf(tmp_path,qt,monkeypatch,rotation):
    store,main,panel,source=prepared(tmp_path,qt,rotation)
    view=panel.pdf_view; original=digest(store.path(main['file_id'])); dl=panel.canvas.data['display_list']
    assert dl and view.full_ready
    with monkeypatch.context() as patch:
        patch.setattr(fitz,'open',lambda *a,**k:pytest.fail('高清重绘应复用绘制指令，不得重新打开PDF'))
        before=view.render_count; panel.zoom.setValue(400)
        assert view.render_count==before,'缩放必须先即时显示'
        wait(panel,qt)
        assert panel.canvas.data['display_list'] is dl
        detail=panel.canvas.hd; assert detail and view.hd_render_count>0
        r=detail['rect']; density=detail['image'].width()/(r[2]-r[0])
        assert density>=view.scale_for(panel.canvas.geometry_info)
        assert density>panel.canvas.data['image'].width()/panel.canvas.data['width']
        assert view.hd_bytes<=view.hd_limit
    assert digest(store.path(main['file_id']))==original
    cleanup(store,panel,qt)

def test_shift_wheel_and_real_spin_arrows_slider_center(tmp_path,qt):
    store,main,panel,source=prepared(tmp_path,qt)
    panel.zoom.setValue(200); wait(panel,qt)
    bar=panel.scroll.horizontalScrollBar(); assert bar.maximum()>0
    bar.setValue(bar.maximum()//2); before=bar.value(); vertical=panel.scroll.verticalScrollBar().value()
    local=QPointF(100,100); global_pos=QPointF(panel.canvas.mapToGlobal(QPoint(100,100)))
    event=QWheelEvent(local,global_pos,QPoint(0,0),QPoint(0,-120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ShiftModifier,Qt.ScrollPhase.NoScrollPhase,False)
    qt.sendEvent(panel.canvas,event)
    assert bar.value()>before and panel.scroll.verticalScrollBar().value()==vertical
    option=QStyleOptionSpinBox(); panel.zoom.initStyleOption(option)
    for control,delta in [(QStyle.SubControl.SC_SpinBoxUp,1),(QStyle.SubControl.SC_SpinBoxDown,-1)]:
        rect=panel.zoom.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,option,control,panel.zoom)
        before=panel.zoom.value(); QTest.mouseClick(panel.zoom,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,rect.center())
        assert panel.zoom.value()==before+delta,'百分比微调箭头必须实际可点击'
    for position,expected in [(-100,25),(0,100),(100,400)]:
        panel.zoom_slider.setValue(position); assert panel.zoom.value()==expected
        assert zoom_slider(expected)==position and slider_zoom(position)==expected
    slider=panel.zoom_slider; slider.setValue(12); slider.setSliderDown(True)
    slider.setSliderPosition(2); assert slider.value()==0 and panel.zoom.value()==100
    slider.setSliderPosition(8); assert slider.value()==8,'拖动离开吸附区后应恢复自由缩放'
    slider.setSliderPosition(-2); assert slider.value()==0
    slider.setSliderDown(False); panel.zoom.setValue(101); assert panel.zoom.value()==101
    assert not any(b.text() in {'100%','复制选中','删除高亮'} for b in panel.findChildren(QPushButton))
    assert '保存批注' in panel.tools_button.text()
    cleanup(store,panel,qt)

def test_highlight_icon_shortcut_reselect_toggle_and_typing_safety(tmp_path,qt):
    store,main,panel,source=prepared(tmp_path,qt)
    panel.canvas.select_chars(0,4); panel.canvas.setFocus(); QTest.keyClick(panel.canvas,Qt.Key.Key_H,Qt.KeyboardModifier.ShiftModifier)
    assert len(store.highlights(main['file_id']))==1
    assert panel.highlight_button.accessibleName()=='取消高亮' and 'Shift+H' in panel.highlight_button.toolTip()
    panel.select_mark(''); panel.canvas.select_chars(0,4)
    assert panel.highlight_button.accessibleName()=='取消高亮'
    panel.highlight_button.click(); assert store.highlights(main['file_id'])==[]
    assert panel.highlight_button.accessibleName()=='高亮'
    image=panel.highlight_button.icon().pixmap(24,24).toImage()
    assert sum(1 for x in range(image.width()) for y in range(image.height()) if (c:=image.pixelColor(x,y)).alpha()>200 and c.red()>200 and 150<c.green()<240 and c.blue()<130)>10
    words=panel.canvas.data['words']; start=next(i for i,w in enumerate(words) if w['text']=='Control')
    panel.canvas.select_words(start,start+8); panel.highlight_button.click()
    saved=store.highlights(main['file_id'])[0]
    chars=panel.canvas.data['characters']; indices=[i for i,c in enumerate(chars) if any(r['rect'][0]-.5<=(c['rect'][0]+c['rect'][2])/2<=r['rect'][2]+.5 and r['rect'][1]-.5<=(c['rect'][1]+c['rect'][3])/2<=r['rect'][3]+.5 for r in saved['anchors'])]
    panel.select_mark(''); panel.canvas.select_chars(min(indices),max(indices)); panel.highlight_button.click()
    assert not store.highlights(main['file_id']),'重新拖选由多个词组成的旧高亮也应取消，不能新增重叠高亮'
    panel.canvas.select_chars(1,3); panel.highlight_button.click(); assert len(store.highlights(main['file_id']))==1
    panel.note.setFocus(); panel.note.clear(); QTest.keyClick(panel.note,Qt.Key.Key_H,Qt.KeyboardModifier.ShiftModifier)
    assert panel.note.text().lower()=='h' and len(store.highlights(main['file_id']))==1
    panel.canvas.setFocus(); QTest.keyClick(panel.canvas,Qt.Key.Key_H,Qt.KeyboardModifier.ShiftModifier)
    assert not store.highlights(main['file_id'])
    cleanup(store,panel,qt)

@pytest.mark.parametrize('rotation',[0,90])
def test_page_comment_drag_resize_font_edit_persist_and_native_export(tmp_path,qt,monkeypatch,rotation):
    store,main,panel,source=prepared(tmp_path,qt,rotation)
    panel.canvas.select_chars(0,8); panel.note.setText('合成测试批注：检查实验条件\n第二行'); panel.highlight_button.click()
    mark=store.highlights(main['file_id'])[0]; mid=mark['mark_id']; box=panel.canvas.note_boxes[mid]
    original=copy.deepcopy(box.style['rect']); position=QPoint(15,8)
    QTest.mousePress(box,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position)
    QTest.mouseMove(box,position+QPoint(-55,50),20); QTest.mouseRelease(box,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position+QPoint(-55,50))
    moved=store.highlights(main['file_id'])[0]['appearance']; assert moved['rect']!=original
    box=panel.canvas.note_boxes[mid]; w=box.width(); h=box.height(); handle=QPoint(w-3,h-3)
    QTest.mousePress(box,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,handle)
    QTest.mouseMove(box,handle+QPoint(30,35),20); QTest.mouseRelease(box,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,handle+QPoint(30,35))
    resized=store.highlights(main['file_id'])[0]['appearance']; assert resized['rect'][3]-resized['rect'][1]>moved['rect'][3]-moved['rect'][1]
    panel.note_font.setValue(16); panel.change_note_font(color='#cc2233')
    appearance=store.highlights(main['file_id'])[0]['appearance']; assert appearance['font_size']==16 and appearance['font_color']=='#cc2233'
    # Editing text must preserve geometry/font settings, including multiline content.
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog,'getMultiLineText',lambda *a:('更新后的中文批注\n第二行',True))
    panel.edit_page_note(mid); assert store.highlights(main['file_id'])[0]['appearance']==appearance
    panel.zoom.setValue(200); wait(panel,qt)
    assert panel.canvas.note_boxes[mid].style==appearance
    output=tmp_path/'页内批注.pdf'; report=save_annotated_pdf(store,main['file_id'],output)
    assert report['note_box_count']==1
    with fitz.open(output) as doc:
        page=doc[0]; note=next(a for a in page.annots() if a.type[0]==fitz.PDF_ANNOT_FREE_TEXT)
        assert note.info['content']=='更新后的中文批注\n第二行'
        assert list(note.rect*page.rotation_matrix)==pytest.approx(appearance['rect'],abs=1)
        assert '#cc2233' in doc.xref_get_key(note.xref,'DS')[1].lower()
        appearance_stream=doc.xref_stream(int(doc.xref_get_key(note.xref,'AP/N')[1].split()[0]))
        assert b'16 Tf' in appearance_stream and b'.8 .13333334 .2 rg' in appearance_stream
        doc[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(tmp_path/'明确标注中文批注渲染.png')
    archive=tmp_path/'带批注样式的备份.zip'; store.backup(archive); restore(archive,tmp_path/'恢复')
    with Store(tmp_path/'恢复') as reopened: assert reopened.highlights(main['file_id'])[0]['appearance']==appearance
    assert digest(store.path(main['file_id']))==store.file(main['file_id'])['sha256']
    cleanup(store,panel,qt)

def test_v3_migration_preserves_comments_and_rejects_bad_style(tmp_path):
    source=fixtures(tmp_path/'迁移合成文档'); root=tmp_path/'旧版本库'
    with Store(root) as store:
        item=store.import_file(source['main']); mid=store.save_highlight(item['file_id'],'pdf','SYNTHETIC',[{'page_index':0,'rect':[40,50,100,65]}],note='旧批注')
    with closing(sqlite3.connect(root/'library.sqlite')) as db,db:
        db.execute('ALTER TABLE highlights DROP COLUMN appearance'); db.execute('PRAGMA user_version=3')
    with Store(root) as store:
        assert store.db.execute('PRAGMA user_version').fetchone()[0]==DB_VERSION
        mark=store.highlights(item['file_id'])[0]; assert mark['note']=='旧批注' and mark['appearance']=={}
        assert list((root/'migrations').glob('library-v3-*.sqlite'))
        style={'page_index':0,'rect':[100,100,240,180],'font_size':12,'font_color':'#123456'}
        for wrong in [dict(style,font_size=float('nan')),dict(style,font_color='invalid'),dict(style,page_index=99),dict(style,rect=[0,0,1,1])]:
            with pytest.raises(ValueError): store.save_note_style(item['file_id'],mid,wrong)
        store.save_note_style(item['file_id'],mid,style)
        store.save_highlight(item['file_id'],'pdf',mark['text'],mark['anchors'],'blue','改写内容',mid)
        assert store.highlights(item['file_id'])[0]['appearance']==style

def test_new_library_child_existing_library_collision_and_memory(tmp_path):
    chosen=tmp_path/'用户选中的文件夹'; chosen.mkdir(); (chosen/'其他文件.txt').write_text('保留',encoding='utf-8')
    memory=StartupMemory(tmp_path/'程序',tmp_path/'设置')
    result=memory.choose_directory(lambda *a:str(chosen)); assert result==str(chosen/DATA_FOLDER)
    with Store(result): pass
    memory.remember(result); assert memory.choose_directory(lambda *a:pytest.fail('无需重新选择'))==result
    assert not (chosen/'library.sqlite').exists() and (chosen/'其他文件.txt').exists()
    assert library_in_folder(chosen)==result and library_in_folder(result)==result
    occupied=tmp_path/'冲突父目录'; (occupied/DATA_FOLDER).mkdir(parents=True); (occupied/DATA_FOLDER/'不要改.txt').write_text('保留',encoding='utf-8')
    assert library_in_folder(occupied)==str(occupied/f'{DATA_FOLDER} (2)')
    assert library_in_folder('')==''
