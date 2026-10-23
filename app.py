import argparse
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="钙钛矿文献助手")
    parser.add_argument("--data", help="数据目录；省略时自动打开上次目录，不写入注册表")
    parser.add_argument("--self-test", metavar="OUTPUT", help="运行明确标注的合成文档端到端自检")
    parser.add_argument("--smoke-test", metavar="OUTPUT", help="显示GUI、PDF预览并保存自检截图后退出")
    args = parser.parse_args()
    if args.self_test:
        from perovskite.selftest import run_selftest
        try:
            run_selftest(args.self_test)
            return 0
        except Exception:
            import traceback
            root = Path(args.self_test); root.mkdir(parents=True, exist_ok=True)
            (root / "selftest-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QIcon
    application = QApplication(sys.argv[:1])
    from perovskite.theme import apply_theme
    apply_theme(application)
    application.setApplicationName("钙钛矿文献助手")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "logo-book-octahedron.ico")))
    if args.smoke_test:
        import socket
        def offline(*a, **k):
            raise RuntimeError("GUI离线验收禁止网络连接")
        socket.create_connection = offline
        from perovskite.selftest import run_selftest
        report = run_selftest(args.smoke_test)
        args.data = report["data_dir"]
    from perovskite.startup import StartupMemory,library_in_folder
    memory=StartupMemory(Path(args.smoke_test)/"启动位置记录",Path(args.smoke_test)/"启动位置备用记录") if args.smoke_test else StartupMemory()
    directory = args.data if args.smoke_test else memory.choose_directory(QFileDialog.getExistingDirectory,args.data)
    if not directory:
        return 0
    from perovskite.gui import Window
    while True:
        try:
            window = Window(directory)
            break
        except Exception as exc:
            QMessageBox.critical(None, "无法打开数据目录", str(exc))
            if args.data or args.smoke_test: return 1
            directory=library_in_folder(QFileDialog.getExistingDirectory(None,"请选择其他存放位置或已有库（原数据不会删除）",str(Path(directory).parent)))
            if not directory: return 0
    def remember_directory(current):
        try: memory.remember(current.store.root)
        except (OSError,ValueError) as exc: current.status.setText(str(exc))

    def connect_directory(current):
        current.directoryRequested.connect(lambda path,w=current:switch_directory(w,path))

    def switch_directory(previous,path):
        nonlocal window
        if previous.worker or previous.reader.export_worker:
            previous.error("请等待当前任务完成后切换数据目录"); return
        previous.reader.stop()
        if previous.reader.active_worker:
            QTimer.singleShot(100,lambda:switch_directory(previous,path)); return
        try:
            replacement=Window(path)
        except Exception as exc:
            previous.error(exc); return
        connect_directory(replacement)
        replacement.resize(previous.size())
        replacement.show(); window=replacement
        remember_directory(replacement)
        previous.close()

    connect_directory(window)
    remember_directory(window)
    if args.smoke_test: window.resize(1280,720)
    window.show()
    if args.smoke_test:
        from perovskite.store import atomic_json
        root = Path(args.smoke_test)
        reader_phase = {"step": 0}
        def screenshot():
            aid = report["article_id"]
            window.article_id = aid; window.refresh()
            fid = next(f["file_id"] for f in window.store.files(aid) if f["role"] == "main")
            reader_phase["automatic_original"]=window.reader.file_id==fid
            reader_phase['tab_preserved']=window.tabs.currentWidget() is window.meta_tab
            category=window.store.create_category('合成验收 · 复现实验')
            window.classify_articles(category,[aid])
            window.store.rename_category(category,'合成验收 · 已分类')
            reader_phase['manual_categories']=window.store.list_articles(category_id=category)[0]['article_id']==aid
            window.tabs.setCurrentWidget(window.story_tab)
            window.grab().save(str(root / "GUI故事.png"))
            fid = next(f["file_id"] for f in window.store.files(aid) if f["role"] == "main")
            window.open_file(fid)
            QTimer.singleShot(1800, finish)
        def finish():
            if window.worker or window.reader.active_worker or window.reader.export_worker or window.reader.pending:
                QTimer.singleShot(300, finish); return
            if reader_phase["step"] == 0:
                words = window.reader.canvas.data["words"]
                chars=window.reader.canvas.data['characters']
                index=next(i for i,c in enumerate(chars) if c['text']=='C')
                window.reader.canvas.select_chars(index,index)
                reader_phase['character_selection']=window.reader.selection_payload()['text']=='C'
                start = next(i for i,w in enumerate(words) if w["text"] == "Control")
                window.reader.canvas.select_words(start,start+8)
                window.reader.note.setText("便携高亮验收：准备复现工艺")
                from PySide6.QtCore import Qt,QEvent
                from PySide6.QtGui import QKeyEvent
                window.reader.canvas.setFocus()
                application.sendEvent(window.reader.canvas,QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_H,Qt.KeyboardModifier.ShiftModifier,'H'))
                reader_phase['highlight_shortcut']=len(window.store.highlights(window.reader.file_id))==1
                window.reader.note_font.setValue(14); window.reader.change_note_font(color='#aa3322')
                window.reader.zoom.setValue(400)
                reader_phase["step"] = 1
                QTimer.singleShot(500,finish); return
            if reader_phase["step"] == 1:
                view=window.reader.pdf_view
                reader_phase['original_commands']=window.reader.canvas.data['display_list']
                reader_phase['original_image']=window.reader.canvas._image.cacheKey()
                reader_phase['vector_detail']=bool(window.reader.canvas.hd) and view.hd_render_count>0 and all(p.data.get('display_list') for p in view.pages)
                reader_phase['yellow_highlight_icon']='取消高亮' in window.reader.highlight_button.toolTip()
                slider=window.reader.zoom_slider; slider.setSliderDown(True); slider.setSliderPosition(2)
                reader_phase['zoom_center_snap']=slider.value()==0 and window.reader.zoom.value()==100
                slider.setSliderDown(False); window.reader.zoom.setValue(200)
                fid = next(f["file_id"] for f in window.store.files(report["article_id"]) if f["role"] == "main")
                window.reader.open_file(fid)
                reader_phase["step"] = 2
                QTimer.singleShot(500,finish); return
            if reader_phase['step']==2:
                import fitz
                multiple=root/'明确标注的连续三页合成测试.pdf'
                with fitz.open() as doc:
                    for i in range(3): doc.new_page().insert_text((40,60),f'SYNTHETIC CONTINUOUS PAGE {i+1} - TEST ONLY')
                    doc.save(multiple)
                item=window.store.import_file(multiple)
                window.reader.open_file(item['file_id'])
                reader_phase['step']=3; QTimer.singleShot(400,finish); return
            if reader_phase['step']==3:
                window.reader.page.stepBy(-1)
                reader_phase['step']=4; QTimer.singleShot(400,finish); return
            if reader_phase['step']==4:
                view=window.reader.pdf_view
                reader_phase['continuous_pages']=len(view.pages)==3 and window.reader.page.value()==2 and view.document.height()>view.scroll.viewport().height()
                reader_phase['full_preload']=view.full_ready and len(view.cache)==3 and all(p.data and not p._image.isNull() for p in view.pages)
                count=view.render_count; width=window.reader.canvas.width()
                window.reader.zoom.setValue(50)
                reader_phase['instant_zoom']=view.render_count==count and window.reader.canvas.width()!=width
                reader_phase['step']=5; QTimer.singleShot(400,finish); return
            if reader_phase['step']==5:
                fid=next(f['file_id'] for f in window.store.files(report['article_id']) if f['role']=='main')
                import time
                started=time.perf_counter()
                window.reader.open_file(fid)
                reader_phase['cache_switch_ms']=round((time.perf_counter()-started)*1000,1)
                view=window.reader.pdf_view
                reader_phase['cross_document_cache']=view.full_ready and view.session_hits>=2 and view.render_count==1 and window.reader.canvas.data['display_list'] is reader_phase['original_commands'] and window.reader.canvas._image.cacheKey()==reader_phase['original_image'] and reader_phase['cache_switch_ms']<1000
                reader_phase['step']=6; QTimer.singleShot(400,finish); return
            if reader_phase['step']==6:
                reader=window.reader; view=reader.pdf_view
                image=reader.canvas._image.cacheKey(); count=view.render_count; hd_count=view.hd_render_count
                reader.browse_button.click(); reader.zoom.setValue(350); view.request_visible()
                reader_phase['fixed_bitmap_mode']=view.fast_browse and reader.browse_button.text()=='高清浏览' and not any(p.hd for p in view.pages) and reader.canvas._image.cacheKey()==image and view.render_count==count and view.hd_render_count==hd_count
                from perovskite.reader import ReaderPanel
                probe=ReaderPanel(window.store)
                reader_phase['browse_mode_saved']=probe.fast_browse and probe.pdf_view.fast_browse and probe.browse_button.text()=='高清浏览'
                probe.close()
                application.processEvents(); window.grab().save(str(root/'GUI加速原文.png'))
                window.theme_action.trigger(); application.processEvents(); window.grab().save(str(root/'GUI深色加速原文.png'))
                window.theme_action.trigger(); reader.zoom.setValue(200); reader.browse_button.click()
                reader_phase['step']=7; QTimer.singleShot(400,finish); return
            window.grab().save(str(root / "GUI原文.png"))
            from perovskite.theme import is_dark
            from PySide6.QtGui import QPalette
            reader=window.reader
            reader_phase['hd_mode_restored']=not reader.fast_browse and not reader.pdf_view.fast_browse and bool(reader.canvas.hd)
            reader_phase['browse_button_layout']=reader.browse_button.size()==reader.focus_button.size() and reader.focus_button.geometry().right()<reader.browse_button.geometry().left()<reader.tools_button.geometry().left()
            reader_phase['reader_title_space']=reader.file_picker.width()>=reader.width()-16 and reader.fit_button.geometry().center().y()==reader.focus_button.geometry().center().y()==reader.tools_button.geometry().center().y()
            window.theme_action.trigger(); application.processEvents()
            reader_phase['dark_mode']=is_dark() and window.theme_action.isChecked() and reader.file_id==window.preview_file and strict_theme_saved()
            window.grab().save(str(root/'GUI深色原文.png'))
            def capture_dialog():
                dialog=application.activeModalWidget()
                if dialog:
                    reader_phase['dark_dialog']=dialog.palette().color(QPalette.ColorRole.Window).lightness()<70
                    dialog.grab().save(str(root/'GUI深色设置.png')); dialog.reject()
            QTimer.singleShot(100,capture_dialog); window.api_settings()
            window.theme_action.trigger(); application.processEvents()
            reader_phase['theme_restored']=not is_dark() and not window.theme_action.isChecked()
            marks=window.store.highlights(window.reader.file_id)
            reader_phase['page_note_persisted']=bool(marks and marks[0]['appearance'].get('font_size')==14 and marks[0]['appearance'].get('font_color')=='#aa3322' and marks[0]['mark_id'] in window.reader.canvas.note_boxes)
            good = window.preview_image.pixmap() is not None and not window.preview_image.pixmap().isNull()
            import PySide6, fitz
            from perovskite.extract import digest
            fid = next(f["file_id"] for f in window.store.files(report["article_id"]) if f["role"] == "main")
            unchanged = digest(window.store.path(fid)) == window.store.file(fid)["sha256"]
            highlight_saved = len(window.store.highlights(fid))==1 and window.reader.marks.topLevelItemCount()==1
            zoom_saved = window.reader.zoom.value()==200 and window.store.reader_state(fid)["zoom"]==200
            from perovskite.pdf_annotations import save_annotated_pdf
            native_copy=save_annotated_pdf(window.store,fid,root/"含标准批注PDF.pdf")
            native_back=save_annotated_pdf(window.store,fid,root/"明确标注的测试文档"/"测试 正文 synthetic.pdf",True)
            with fitz.open(root/"含标准批注PDF.pdf") as annotated:
                native_saved=any(a.info.get("content")=="便携高亮验收：准备复现工艺" for p in annotated for a in (p.annots() or []))
                reader_phase['native_note_box']=any(a.type[0]==fitz.PDF_ANNOT_FREE_TEXT for p in annotated for a in (p.annots() or []))
                annotated[0].get_pixmap().save(root/"原生PDF批注渲染.png")
            native_backup=bool(native_back["backup"]) and digest(native_back["backup"])==window.store.file(fid)["sha256"]
            unchanged=digest(window.store.path(fid))==window.store.file(fid)["sha256"]
            docx = next(f for f in window.store.files(report["article_id"]) if f["path"].endswith(".docx"))
            window.reader.open_file(docx["file_id"])
            cursor=window.reader.doc_text.document().find("Control sample annealed at 100 C for 10 min.")
            window.reader.doc_text.setTextCursor(cursor); window.reader.note.setText("DOCX高亮验收")
            window.reader.save_selection(); window.reader.zoom.setValue(150); window.reader.open_file(docx["file_id"])
            docx_saved=bool(window.reader.doc_text.extraSelections()) and window.reader.zoom.value()==150
            window.grab().save(str(root / "GUI_DOCX.png"))
            from perovskite.exporting import excel_export,markdown
            excel_export(window.store,[report["article_id"]],root/"含高亮导出.xlsx")
            (root/"含高亮阅读卡.md").write_text(markdown(window.store,report["article_id"]),encoding="utf-8")
            window.store.backup(root/"含高亮完整备份.zip")
            automatic_original=reader_phase.get("automatic_original",False)
            def unexpected_picker(*args): raise AssertionError("已记忆的目录不应再次弹出选择窗口")
            startup_memory=StartupMemory(memory.app_directory,memory.fallback_directory).choose_directory(unexpected_picker)==str(window.store.root)
            parent=root/'新库父文件夹'; parent.mkdir(exist_ok=True); child=library_in_folder(parent)
            from perovskite.store import Store
            with Store(child): pass
            reader_phase['startup_child_folder']=not (parent/'library.sqlite').exists() and Path(child).parent==parent.resolve() and (Path(child)/'library.sqlite').exists()
            compact_layout=min(1200,application.primaryScreen().availableGeometry().width()-32)<=window.width()<=1280 and window.height()==720 and window.reader.doc_text.viewport().height()>=400 and window.table.rowHeight(0)==32
            reader_checks={k:reader_phase.get(k,False) for k in ['continuous_pages','instant_zoom','character_selection','tab_preserved','full_preload','manual_categories','vector_detail','zoom_center_snap','page_note_persisted','native_note_box','yellow_highlight_icon','highlight_shortcut','startup_child_folder','reader_title_space','dark_mode','dark_dialog','theme_restored','cross_document_cache','fixed_bitmap_mode','browse_mode_saved','hd_mode_restored','browse_button_layout']}
            reader_checks['cache_switch_ms']=reader_phase['cache_switch_ms']
            atomic_json(root / "gui-report.json", {**reader_checks,"pdf_preview": good, "startup_memory":startup_memory,"automatic_original":automatic_original,"compact_layout":compact_layout,"original_hash_unchanged": unchanged, "native_pdf_annotations":native_saved,"write_back_backup":native_backup,"pdf_highlight_persisted":highlight_saved,"zoom_persisted":zoom_saved,"docx_highlight_persisted":docx_saved,"article_count": window.table.rowCount(), "data_dir": str(window.store.root), "frozen": bool(getattr(sys, "frozen", False)), "qt_runtime": PySide6.__file__, "pdf_runtime": fitz.__file__})
            window.close()
            application.exit(0 if good and unchanged and highlight_saved and zoom_saved and docx_saved and native_saved and native_backup and automatic_original and compact_layout and startup_memory and all(v for k,v in reader_checks.items() if k!='cache_switch_ms') else 1)
        def strict_theme_saved():
            import json
            return json.loads((window.store.root/'config.json').read_text(encoding='utf-8')).get('theme')=='dark'
        QTimer.singleShot(500, screenshot)
    return application.exec()

if __name__ == "__main__":
    raise SystemExit(main())
