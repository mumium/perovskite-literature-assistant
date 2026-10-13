import argparse
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="钙钛矿文献助手")
    parser.add_argument("--data", help="数据目录；省略时每次启动选择，不写入注册表")
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
    directory = args.data or QFileDialog.getExistingDirectory(None, "钙钛矿文献助手：选择或新建数据目录（不要与他人同时编辑）")
    if not directory:
        return 0
    try:
        from perovskite.gui import Window
        window = Window(directory)
    except Exception as exc:
        QMessageBox.critical(None, "无法打开数据目录", str(exc))
        return 1
    window.show()
    if args.smoke_test:
        from perovskite.store import atomic_json
        root = Path(args.smoke_test)
        reader_phase = {"step": 0}
        def screenshot():
            aid = report["article_id"]
            window.article_id = aid; window.refresh()
            window.tabs.setCurrentWidget(window.story_tab)
            window.grab().save(str(root / "GUI故事.png"))
            fid = next(f["file_id"] for f in window.store.files(aid) if f["role"] == "main")
            window.open_file(fid)
            QTimer.singleShot(1800, finish)
        def finish():
            if window.worker or window.reader.active_worker or window.reader.pending:
                QTimer.singleShot(300, finish); return
            if reader_phase["step"] == 0:
                words = window.reader.canvas.data["words"]
                start = next(i for i,w in enumerate(words) if w["text"] == "Control")
                window.reader.canvas.select_words(start,start+8)
                window.reader.note.setText("便携高亮验收：准备复现工艺")
                window.reader.save_selection()
                window.reader.zoom.setValue(200)
                reader_phase["step"] = 1
                QTimer.singleShot(500,finish); return
            if reader_phase["step"] == 1:
                fid = next(f["file_id"] for f in window.store.files(report["article_id"]) if f["role"] == "main")
                window.reader.open_file(fid)
                reader_phase["step"] = 2
                QTimer.singleShot(500,finish); return
            window.grab().save(str(root / "GUI原文.png"))
            good = window.preview_image.pixmap() is not None and not window.preview_image.pixmap().isNull()
            import PySide6, fitz
            from perovskite.extract import digest
            fid = next(f["file_id"] for f in window.store.files(report["article_id"]) if f["role"] == "main")
            unchanged = digest(window.store.path(fid)) == window.store.file(fid)["sha256"]
            highlight_saved = len(window.store.highlights(fid))==1 and window.reader.marks.topLevelItemCount()==1
            zoom_saved = window.reader.zoom.value()==200 and window.store.reader_state(fid)["zoom"]==200
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
            atomic_json(root / "gui-report.json", {"pdf_preview": good, "original_hash_unchanged": unchanged, "pdf_highlight_persisted":highlight_saved,"zoom_persisted":zoom_saved,"docx_highlight_persisted":docx_saved,"article_count": window.table.rowCount(), "data_dir": str(window.store.root), "frozen": bool(getattr(sys, "frozen", False)), "qt_runtime": PySide6.__file__, "pdf_runtime": fitz.__file__})
            window.close()
            application.exit(0 if good and unchanged and highlight_saved and zoom_saved and docx_saved else 1)
        QTimer.singleShot(500, screenshot)
    return application.exec()

if __name__ == "__main__":
    raise SystemExit(main())
