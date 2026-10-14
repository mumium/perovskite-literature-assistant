import html
import json
import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QLockFile
from PySide6.QtGui import QPixmap, QImage, QIcon, QAction, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView, QLineEdit, QComboBox,
    QPushButton, QLabel, QTabWidget, QTextBrowser, QPlainTextEdit, QFileDialog, QMessageBox, QDialog,
    QDialogButtonBox, QFormLayout, QCheckBox, QSpinBox, QScrollArea, QProgressBar, QGroupBox, QHeaderView, QSizePolicy)

from .exchange import preview, apply_result, strict_json, export_package, evidence_checks, semantic_validate
from .exporting import excel_export, markdown
from .schema import META_FIELDS, STORY_FIELDS, TAGS, PROMPT, STORY, PROCESS
from .store import Store, dumps, atomic_json, restore, uid
from .network import crossref, analyze
from .reader import ReaderPanel

ROLE_NAMES = {"main": "正文", "si": "SI", "version": "正文版本", "preprint": "预印本", "other": "其他"}
STATE_NAMES = {"ready": "文字已提取", "partial": "部分页无文字", "no_text": "无文字层", "unsupported": "暂不支持", "error": "解析失败", "duplicate": "重复跳过"}

def esc(value):
    return html.escape(str(value if value is not None else "未报告"))

def shown(value):
    return "未报告" if value is None else str(value)

class Worker(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)
    def __init__(self, function):
        super().__init__()
        self.function, self.cancelled = function, threading.Event()
    def run(self):
        try:
            result = self.function(self.cancelled.is_set, self.progress.emit)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

class TextDialog(QDialog):
    def __init__(self, title, text, parent=None, editable=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        self.editor.setReadOnly(not editable)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | (QDialogButtonBox.StandardButton.Cancel if editable else QDialogButtonBox.StandardButton.NoButton))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

class Window(QMainWindow):
    directoryRequested=Signal(str)
    def __init__(self, data_dir):
        super().__init__()
        self.setWindowTitle("钙钛矿文献助手")
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parent.parent / "assets" / "logo-book-octahedron.ico")))
        available=QApplication.primaryScreen().availableGeometry()
        self.resize(min(1680,available.width()-32),min(980,available.height()-48))
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(root / ".library.lock"))
        self.lock.setStaleLockTime(0)
        if not self.lock.tryLock(50):
            raise ValueError("该数据目录正在使用。请勿同时编辑同一数据目录；关闭另一程序后再试。")
        try:
            self.store = Store(root)
        except Exception:
            self.lock.unlock()
            raise
        self.article_id = None
        self.worker = None
        self.busy_controls = []
        self.preview_file = None
        self.preview_evidence = None
        self.setAcceptDrops(True)
        self.setup_ui()
        self.refresh()

    def button(self, label, callback, layout, busy=True):
        b = QPushButton(label)
        b.clicked.connect(callback)
        layout.addWidget(b)
        if busy:
            self.busy_controls.append(b)
        return b

    def setup_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(6,4,6,4); outer.setSpacing(4)
        self.banner = banner = QLabel("钙钛矿文献助手")
        banner.setStyleSheet("font-size:18px;font-weight:600;color:#214d62;padding:2px")
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(Path(__file__).resolve().parent.parent / "assets" / "logo-book-octahedron.png")).scaled(30, 30, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        brand.addWidget(logo)
        brand.addWidget(banner)
        self.path_label = QLabel("数据目录：" + str(self.store.root) + "   ｜请勿同时编辑同一数据目录")
        self.path_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.path_label.setToolTip(self.path_label.text())
        self.path_label.setStyleSheet("color:#637382;font-size:11px")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        brand.addWidget(self.path_label,1); outer.addLayout(brand)
        bar = QHBoxLayout()
        outer.addLayout(bar)
        for label, callback in [("导入文件", self.import_files), ("导入文件夹", self.import_folder), ("关联附件", self.pair_file), ("导出分析包", self.export_analysis), ("导回结果", self.import_analysis), ("复制AI提示词", self.copy_prompt), ("API分析", self.api_analyze), ("比较 / Excel", self.export_excel)]:
            self.button(label, callback, bar)
        self.button("更多", self.more_menu, bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split = split
        self._reading_mode = False
        outer.addWidget(split, 1)
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderLabel("专题 / 标签 / 处理状态")
        self.sidebar.setMaximumWidth(250)
        self.sidebar.setMinimumWidth(100)
        self.sidebar.setUniformRowHeights(True); self.sidebar.setIndentation(12)
        all_item = QTreeWidgetItem(["全部文献"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("", ""))
        self.sidebar.addTopLevelItem(all_item)
        for title, field, values in [("处理状态", "state", ["待配对确认", "解析需关注", "故事待分析", "工艺待分析", "引用待核验", "人工待核验", "人工已核验"]), ("专题标签", "tags", TAGS)]:
            parent = QTreeWidgetItem([title])
            self.sidebar.addTopLevelItem(parent)
            for value in values:
                item = QTreeWidgetItem([value])
                item.setData(0, Qt.ItemDataRole.UserRole, (field, value))
                parent.addChild(item)
            parent.setExpanded(True)
        self.sidebar.itemClicked.connect(self.sidebar_filter)
        split.addWidget(self.sidebar)
        center = QWidget()
        self.center_panel = center
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4); center.setMinimumWidth(240)
        self.search = QLineEdit()
        self.search.setPlaceholderText("全文搜索：正文、SI、阅读卡、工艺、人工笔记")
        self.search.returnPressed.connect(self.refresh)
        cl.addWidget(self.search)
        filterbar = QHBoxLayout()
        self.filter_field = QComboBox()
        for key in ["journal", "year", "authors", "material", "structure", "topic", "tags", "state"]:
            self.filter_field.addItem(META_FIELDS.get(key, "状态"), key)
        self.filter_value = QLineEdit()
        self.filter_value.setPlaceholderText("筛选值")
        self.filter_value.returnPressed.connect(self.refresh)
        filterbar.addWidget(self.filter_field)
        filterbar.addWidget(self.filter_value)
        self.button("搜索", self.refresh, filterbar, False)
        self.button("清空", self.clear_filters, filterbar, False)
        cl.addLayout(filterbar)
        self.count_label = QLabel()
        countbar=QHBoxLayout(); countbar.addWidget(self.count_label,1)
        self.list_details=QPushButton("详情列"); self.list_details.setCheckable(True)
        self.list_details.setToolTip("展开或收起期刊、SI数量及处理状态列；悬停标题也可查看这些信息")
        self.list_details.toggled.connect(lambda visible:[self.table.setColumnHidden(col,not visible) for col in [1,2,3]])
        countbar.addWidget(self.list_details); cl.addLayout(countbar)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文献标题", "期刊 / 年份", "SI数", "处理状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 86)
        self.table.setColumnWidth(2, 36)
        self.table.setColumnWidth(3, 104)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(30)
        for col in [1,2,3]: self.table.setColumnHidden(col,True)
        self.table.itemSelectionChanged.connect(self.select_article)
        self.table.itemClicked.connect(lambda _:self.show_article_original())
        cl.addWidget(self.table, 1)
        help_label=QLabel("拖入导入 · Ctrl/Shift多选比较")
        help_label.setToolTip("配对建议仅供确认，同DOI不同版本均保留；悬停单元格可查看完整标题和状态")
        cl.addWidget(help_label)
        split.addWidget(center)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setStretchFactor(2,3)
        split.setSizes([128,380,1000])
        self.meta_tab = QWidget()
        ml = QVBoxLayout(self.meta_tab)
        self.meta = QTextBrowser()
        self.meta.setOpenExternalLinks(False)
        ml.addWidget(self.meta, 1)
        mb = QHBoxLayout()
        self.button("修订元数据", self.edit_metadata, mb)
        self.button("候选与来源", self.show_candidates, mb)
        self.button("Crossref候选", self.lookup_crossref, mb)
        ml.addLayout(mb)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("人工笔记（点击保存）")
        self.notes.setMaximumHeight(130)
        ml.addWidget(self.notes)
        self.button("保存笔记", self.save_notes, ml)
        self.tabs.addTab(self.meta_tab, "文献信息")
        self.story_tab = QWidget()
        sl = QVBoxLayout(self.story_tab)
        self.story = QTextBrowser()
        self.story.setOpenLinks(False)
        self.story.anchorClicked.connect(lambda url: self.jump_evidence(url.toString()))
        sl.addWidget(self.story)
        sb = QHBoxLayout()
        self.button("人工修订 / 核验故事", lambda: self.edit_section("story"), sb)
        self.button("导出Markdown", self.export_markdown, sb)
        sl.addLayout(sb)
        self.tabs.addTab(self.story_tab, "核心故事")
        self.process_tab = QWidget()
        pl = QVBoxLayout(self.process_tab)
        self.process = QTreeWidget()
        self.process.setHeaderLabels(["样品 / 工艺步骤 / 参数", "原文值与单位", "标准化值与单位", "基准 / 换算 / 冲突", "证据"])
        self.process.setColumnWidth(0, 240)
        self.process.itemDoubleClicked.connect(self.tree_evidence)
        pl.addWidget(self.process)
        self.button("人工修订 / 核验工艺性能", lambda: self.edit_section("process"), pl)
        self.tabs.addTab(self.process_tab, "工艺")
        self.performance_tab = QWidget()
        pfl = QVBoxLayout(self.performance_tab)
        pfl.addWidget(QLabel("按样品和测量条件展示。稳定性条件不同不能直接统一排名。"))
        self.performance = QTreeWidget()
        self.performance.setHeaderLabels(["样品 / 测量类型 / 参数", "原文值", "标准化值", "测量条件 / 冲突", "证据"])
        self.performance.setColumnWidth(0, 230)
        self.performance.itemDoubleClicked.connect(self.tree_evidence)
        pfl.addWidget(self.performance)
        self.tabs.addTab(self.performance_tab, "性能与稳定性")
        self.evidence_tab = QWidget()
        el = QVBoxLayout(self.evidence_tab)
        el.addWidget(QLabel("引用匹配只检查文本存在；人工核验需阅读原文并检查结论。双击证据跳转原文。"))
        self.evidence = QTreeWidget()
        self.evidence.setHeaderLabels(["证据ID", "文件 / 位置 / 图表", "原文片段", "引用 / 人工核验"])
        self.evidence.setColumnWidth(2, 250)
        self.evidence.itemDoubleClicked.connect(lambda item, _: self.jump_evidence(item.text(0)))
        el.addWidget(self.evidence)
        self.tabs.addTab(self.evidence_tab, "证据核验")
        self.attach_tab = QWidget()
        al = QVBoxLayout(self.attach_tab)
        self.attachments = QTreeWidget()
        self.attachments.setHeaderLabels(["附件", "角色", "解析状态", "配对与解析说明"])
        self.attachments.setColumnWidth(0, 220)
        self.attachments.setColumnWidth(3, 330)
        self.attachments.itemDoubleClicked.connect(lambda *_: self.open_attachment())
        al.addWidget(self.attachments)
        ab = QHBoxLayout()
        for label, callback in [("选择附件关联…", self.pair_file), ("查看", self.open_attachment), ("修改角色 / 确认", self.edit_file_role), ("解除关联", self.detach_file), ("重试解析", self.retry_file), ("待确认队列", self.pending_pairs)]:
            self.button(label, callback, ab)
        al.addLayout(ab)
        self.tabs.addTab(self.attach_tab, "附件与配对")
        self.viewer_tab = QWidget()
        vl = QVBoxLayout(self.viewer_tab)
        vl.setContentsMargins(0,0,0,0)
        self.reader = ReaderPanel(self.store, self)
        self.reader.fileRequested.connect(self.open_file)
        self.reader.error.connect(self.error)
        self.reader.focusRequested.connect(self.toggle_reader_mode)
        self.reader.changed.connect(lambda _: self.status.setText("原文高亮与批注已自动保存，可全文检索"))
        self.busy_controls.append(self.reader)
        self.preview_image = self.reader.canvas
        self.doc_text = self.reader.doc_text
        self.page = self.reader.page
        self.scroll = self.reader.scroll
        self.viewer_label = self.reader.label
        vl.addWidget(self.reader)
        self.tabs.addTab(self.viewer_tab, "原文查看")
        bottom = QHBoxLayout()
        self.progressbar = QProgressBar()
        self.progressbar.setRange(0, 1)
        self.progressbar.setValue(0)
        self.status = QLabel("就绪 · 离线模式，无启动网络请求")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_task)
        bottom.addWidget(self.status, 2)
        bottom.addWidget(self.progressbar, 1)
        bottom.addWidget(self.cancel_button)
        outer.addLayout(bottom)
        self.setStyleSheet("QMainWindow{background:#f5f7fa} QPushButton{padding:3px 6px;font-size:12px} QTabBar::tab{padding:5px 6px;font-size:12px} QTabWidget::pane{border:1px solid #ccd5dc} QTreeWidget,QTableWidget,QTextBrowser,QPlainTextEdit{background:white;alternate-background-color:#edf3f7} QHeaderView::section{background:#e4edf3;padding:3px;border:0px;font-size:12px} QLineEdit,QComboBox,QSpinBox{padding:2px;font-size:12px} QProgressBar{max-height:16px} QSplitter::handle{background:#d7e0e7}")

    def error(self, text):
        QMessageBox.warning(self, "需要处理", str(text))

    def need_article(self):
        if not self.article_id:
            self.error("请先选择一篇文献")
            return False
        return True

    def run_task(self, label, function, callback=None, refresh=True):
        if self.reader.export_worker:
            self.error("PDF正在保存，请等待结束或取消保存")
            return
        if self.worker:
            self.error("请等待当前任务完成或取消")
            return
        self.status.setText(label)
        self.progressbar.setRange(0, 0)
        self.cancel_button.setEnabled(True)
        for control in self.busy_controls:
            control.setEnabled(False)
        worker = Worker(function)
        self.worker = worker
        worker.progress.connect(self.on_progress)
        worker.failed.connect(self.error)
        result_box = []
        worker.done.connect(lambda result: result_box.append(result))
        def finish():
            self.worker = None
            self.cancel_button.setEnabled(False)
            for control in self.busy_controls:
                control.setEnabled(True)
            self.progressbar.setRange(0, 1)
            self.progressbar.setValue(1)
            self.status.setText("任务结束 · 结果待人工核验")
            if refresh:
                self.refresh()
            if result_box and callback:
                try:
                    callback(result_box[0])
                except Exception as exc:
                    self.error(exc)
            worker.deleteLater()
        worker.finished.connect(finish)
        worker.start()

    def on_progress(self, done, total, text):
        self.status.setText(text)
        self.progressbar.setRange(0, total)
        self.progressbar.setValue(done)

    def cancel_task(self):
        if self.worker:
            self.worker.cancelled.set()
            self.status.setText("正在取消；当前API请求最多等待设置的超时时间，已完成任务保留")

    def selected_ids(self):
        return [self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in self.table.selectionModel().selectedRows()]

    def refresh(self):
        previous = self.article_id
        filters = {self.filter_field.currentData(): self.filter_value.text()}
        rows = self.store.list_articles(self.search.text(), filters)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        selected_row = None
        for i, a in enumerate(rows):
            m = a["metadata"]
            vals = [m.get("title_zh") or m.get("title") or "未命名文献", f"{m.get('journal') or '未报告'} / {m.get('year') or '未报告'}", str(sum(f["role"] == "si" for f in self.store.files(a["article_id"]))), a["state"]]
            for j, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setToolTip("\n".join([vals[0],vals[1],"SI数量："+vals[2],vals[3]]) if j==0 else value)
                if j == 0:
                    item.setData(Qt.ItemDataRole.UserRole, a["article_id"])
                self.table.setItem(i, j, item)
            self.table.setRowHeight(i, 32)
            if a["article_id"] == previous:
                selected_row = i
        self.table.blockSignals(False)
        self.count_label.setText(f"{len(rows)} 篇文献 · 本地全文与字段检索")
        if rows:
            self.table.selectRow(selected_row if selected_row is not None else 0)
        else:
            self.article_id = None
            self.meta.setPlainText("请选择或导入文献。导入后在附件页确认正文及SI，再导出分析包。")
            self.story.clear()
            self.process.clear()
            self.performance.clear()
            self.evidence.clear()
            self.attachments.clear()
            self.notes.clear()
            self.reader.set_files([]); self.reader.clear_file("请选择或导入文献")
            self.preview_file=self.preview_evidence=None

    def clear_filters(self):
        self.search.clear()
        self.filter_value.clear()
        self.refresh()

    def sidebar_filter(self, item, _):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            field, value = data
            if field:
                self.filter_field.setCurrentIndex(self.filter_field.findData(field))
            self.filter_value.setText(value)
            self.refresh()

    def select_article(self):
        ids = self.selected_ids()
        if not ids:
            return
        previous=self.article_id
        current = self.table.currentRow()
        self.article_id = self.table.item(current, 0).data(Qt.ItemDataRole.UserRole) if current >= 0 else ids[0]
        a = self.store.article(self.article_id)
        m = a["metadata"]
        parts = [f"<h2>{esc(m.get('title_zh') or m.get('title'))}</h2>", f"<p>文章ID：{esc(self.article_id)}</p><table cellpadding='5'>"]
        for k, label in META_FIELDS.items():
            value = m.get(k)
            if isinstance(value, list):
                value = "; ".join(value) or "未报告"
            parts.append(f"<tr><td><b>{esc(label)}</b></td><td>{esc(value)}</td></tr>")
        parts.append("</table><p>本地提取和Crossref仅产生候选。来源与冲突见“候选与来源”。人工修订保留历史。</p>")
        self.meta.setHtml("".join(parts))
        self.notes.setPlainText(a["notes"])
        self.story.clear()
        self.evidence.clear()
        for name in ["story", "process"]:
            section = self.store.section(self.article_id, name)
            if not section:
                if name == "story":
                    self.story.setPlainText("故事待分析。确认附件后导出分析包，在联网电脑分析，再导回JSON结果。")
                continue
            verify_label = "人工已核验" if section["human_verified"] else "人工待核验"
            if name == "story":
                parts = [f"<p><b>{verify_label}</b> · 原文匹配仅证明引用存在。</p>"]
                for key, title in STORY_FIELDS.items():
                    parts.append(f"<h3>{title}</h3>")
                    for claim in section["content"].get(key, []):
                        label = {"author_claim": "作者主张", "direct_result": "直接结果", "ai_inference": "推测或建议"}[claim["kind"]]
                        links = " ".join(f"<a href='{esc(e)}'>{esc(e)}</a>" for e in claim["evidence_ids"])
                        parts.append(f"<p><b>[{label}]</b> {esc(claim['text'])}<br>{links}</p>")
                self.story.setHtml("".join(parts))
            checks = {c["evidence_id"]: c for c in section["checks"]}
            for e in section["evidence"]:
                item = QTreeWidgetItem([e["evidence_id"], f"{e['file_id'][:8]} / {e['loc_id']} / {e['figure'] or '未报告'}", e["quote"], ("原文匹配" if checks.get(e["evidence_id"], {}).get("matched") else "引用待核验") + " / " + verify_label])
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                self.evidence.addTopLevelItem(item)
        self.populate_process()
        self.attachments.clear()
        for f in self.store.files(self.article_id):
            item = QTreeWidgetItem([f["name"], ROLE_NAMES[f["role"]], STATE_NAMES.get(f["status"], f["status"]), ("已确认：" if f["confirmed"] else "待确认：") + f["pairing"] + "；" + f["reason"]])
            item.setData(0, Qt.ItemDataRole.UserRole, f["file_id"])
            for col in range(4):
                item.setToolTip(col, item.text(col))
            self.attachments.addTopLevelItem(item)
        files=self.store.files(self.article_id)
        self.reader.set_files(files)
        if previous!=self.article_id or self.reader.file_id not in {f['file_id'] for f in files}:
            self.show_article_original()
        elif self.reader.file_id:
            self.reader.file_picker.blockSignals(True)
            self.reader.file_picker.setCurrentIndex(self.reader.file_picker.findData(self.reader.file_id))
            self.reader.file_picker.blockSignals(False)

    def show_article_original(self):
        if not self.article_id or self.worker: return
        files=self.store.files(self.article_id)
        priority={"main":0,"version":1,"preprint":2,"si":3,"other":4}
        candidates=sorted((f for f in files if Path(f['path']).suffix.lower() in {'.pdf','.docx'} and f['status'] in {'ready','partial','no_text'}),key=lambda f:priority.get(f['role'],5))
        self.tabs.setCurrentWidget(self.viewer_tab)
        if not candidates:
            self.preview_file=self.preview_evidence=None
            reason="；".join(f["name"]+"："+f["reason"] for f in files)
            self.reader.clear_file(reason or "当前文献没有附件，请在附件与配对中关联文件")
            return
        fid=candidates[0]['file_id']
        if self.reader.file_id!=fid or self.reader.evidence is not None or (self.reader.kind=='pdf' and self.reader.canvas.data is None and not self.reader.active_worker):
            self.open_file(fid)

    def populate_process(self):
        self.process.clear()
        self.performance.clear()
        section = self.store.section(self.article_id, "process")
        if not section:
            self.process.addTopLevelItem(QTreeWidgetItem(["工艺待分析"])); return
        def param_item(p):
            item = QTreeWidgetItem([p["name"], f"{shown(p['original_value'])} {p['original_unit'] or ''}", f"{shown(p['normalized_value'])} {p['normalized_unit'] or ''}", f"基准：{p['basis'] or '未报告'}；{p['conversion_note'] or ''}；冲突：{p['conflict_group'] or '无'}", ", ".join(p["evidence_ids"])])
            item.setData(0, Qt.ItemDataRole.UserRole, p["evidence_ids"])
            return item
        for sample in section["content"]["samples"]:
            item = QTreeWidgetItem([f"{sample['label']} ({sample['sample_id']}) / {sample['group']}"])
            self.process.addTopLevelItem(item)
            for step in sorted(sample["steps"], key=lambda s: s["order"]):
                child = QTreeWidgetItem([f"{step['order']}. {step['category']}", step["description"], "", "", ", ".join(step["evidence_ids"])])
                child.setData(0, Qt.ItemDataRole.UserRole, step["evidence_ids"])
                item.addChild(child)
                for p in step["parameters"]:
                    child.addChild(param_item(p))
        for rec in section["content"]["performance"]:
            item = QTreeWidgetItem([f"{rec['sample_id']} / {rec['measurement_kind']}", "", "", dumps(rec["conditions"]), ", ".join(rec["evidence_ids"])])
            item.setData(0, Qt.ItemDataRole.UserRole, rec["evidence_ids"])
            self.performance.addTopLevelItem(item)
            for p in rec["values"]:
                item.addChild(param_item(p))
        self.process.expandAll()
        self.performance.expandAll()

    def import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择正文及SI", "", "文献附件 (*.pdf *.docx *.doc)")
        if paths:
            self.import_paths(paths)

    def import_folder(self):
        path = QFileDialog.getExistingDirectory(self, "递归导入文件夹")
        if path:
            self.import_paths([path])

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.worker:
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])

    def import_paths(self, paths):
        root = self.store.root
        def work(cancel, progress):
            files, seen, report = [], set(), []
            for raw in paths:
                path = Path(raw)
                if path.is_dir() and path.resolve().is_relative_to(root):
                    report.append({"file": str(path), "status": "error", "reason": "不能递归导入当前数据目录"})
                    continue
                candidates = path.rglob("*") if path.is_dir() else [path]
                for p in candidates:
                    if cancel():
                        return report
                    if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".doc"} and p.resolve() not in seen and not p.resolve().is_relative_to(root):
                        seen.add(p.resolve()); files.append(p)
            with Store(root) as store:
                for i, p in enumerate(files):
                    if cancel():
                        break
                    progress(i, len(files), f"导入 {i+1}/{len(files)}：{p.name}")
                    try:
                        report.append({"file": p.name, **store.import_file(p, cancel)})
                    except InterruptedError:
                        report.append({"file": p.name, "status": "cancelled", "reason": "已取消，原文件不受影响"}); break
                    except Exception as exc:
                        report.append({"file": p.name, "status": "error", "reason": str(exc)})
                atomic_json(root / "results" / f"import-report-{uid()}.json", report)
            return report
        self.run_task("扫描并导入附件", work, lambda r: TextDialog("导入报告 · 在附件页确认配对", dumps(r), self).exec())

    def chosen_file(self):
        item = self.attachments.currentItem()
        if not item:
            self.error("请在附件页选择一个文件")
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def pair_file(self):
        if not self.need_article(): return
        if self.worker or self.reader.export_worker:
            self.error("请等待当前任务或PDF保存结束后再关联附件"); return
        aid=self.article_id
        paths,_=QFileDialog.getOpenFileNames(self,"选择要关联到当前文献的附件（可多选）","","文献附件 (*.pdf *.docx *.doc)")
        if not paths: return
        article=self.store.article(aid)
        title=article["metadata"].get("title_zh") or article["metadata"].get("title") or "未命名文献"
        dialog=QDialog(self); dialog.setWindowTitle("关联到当前文献"); dialog.resize(620,240)
        form=QFormLayout(dialog)
        target=QLabel(title); target.setWordWrap(True); target.setToolTip(aid)
        form.addRow("目标文献",target)
        selected=QLabel(f"已选择 {len(paths)} 个文件："+"、".join(Path(p).name for p in paths[:2])+(" 等" if len(paths)>2 else ""))
        selected.setWordWrap(True); selected.setToolTip("\n".join(paths)); form.addRow(selected)
        role=QComboBox(); role.setObjectName("attachment_role")
        for key,name in ROLE_NAMES.items(): role.addItem(name,key)
        role.setCurrentIndex(role.findData("si")); form.addRow("所选附件角色",role)
        explanation=QLabel("新文件会复制到数据目录；相同文件复用。已属于其他文献的附件会移入当前文献，原关联解除；分析内容保留，但需重新核验。")
        explanation.setWordWrap(True); form.addRow(explanation)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("关联所选附件")
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if not dialog.exec(): return
        self.attach_paths_to_article(aid,paths,role.currentData())

    def attach_paths_to_article(self,aid,paths,role="si"):
        root=self.store.root
        def work(cancel,progress):
            with Store(root) as store: return store.attach_paths(aid,paths,role,cancel,progress)
        def completed(report):
            self.article_id=aid; self.refresh(); self.tabs.setCurrentWidget(self.attach_tab)
            success=[r for r in report if r["status"] in {"associated","unchanged"}]
            problems=[r for r in report if r["status"] in {"error","cancelled"} or r.get("parse_status") not in {"ready",None}]
            self.status.setText(f"当前文献已关联 {len(success)} 个附件（复用 {sum(r['reused'] for r in success)} 个）"+(f"；{len(problems)} 项需关注" if problems else ""))
            if problems:
                lines=[f"{r['file']}\n{STATE_NAMES.get(r.get('parse_status'),r['status'])}：{r['reason']}" for r in problems]
                TextDialog("附件关联报告 · 已完成项保留，可重试失败文件","\n\n".join(lines),self).exec()
        self.run_task("从电脑选择附件并关联到当前文献",work,completed,False)

    def edit_file_role(self):
        fid = self.chosen_file()
        if not fid:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("修改附件角色 / 确认")
        dialog.resize(620, 220)
        form = QFormLayout(dialog)
        role = QComboBox()
        for key, name in ROLE_NAMES.items():
            role.addItem(name, key)
        current = self.store.db.execute("SELECT role FROM links WHERE file_id=?", (fid,)).fetchone()[0]
        role.setCurrentIndex(role.findData(current))
        name=QLabel(self.store.file(fid)["name"]); name.setWordWrap(True)
        form.addRow("当前附件",name)
        form.addRow("本文件角色", role)
        form.addRow(QLabel("同DOI版本均保留。更改关联会使原分析核验失效，但保留内容与历史。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            prior=self.store.db.execute("SELECT * FROM links WHERE file_id=?",(fid,)).fetchone()
            if not prior["confirmed"] or prior["role"]!=role.currentData():
                self.store.associate(fid, self.article_id, role.currentData())
            self.refresh(); self.tabs.setCurrentWidget(self.attach_tab)

    def detach_file(self):
        fid = self.chosen_file()
        if fid:
            self.article_id = self.store.detach(fid); self.refresh()

    def pending_pairs(self):
        lines = self.store.rows("SELECT p.*,f.name FROM proposals p JOIN files f USING(file_id) WHERE resolved=0")
        TextDialog("待确认建议 · 先点目标文献，再点关联附件选择原文件", dumps(lines) if lines else "暂无候选建议。请先点目标文献，再点关联附件选择原文件；已有关联可在附件页确认角色。", self).exec()

    def retry_file(self):
        fid = self.chosen_file()
        if fid:
            root = self.store.root
            def work(cancel, progress):
                with Store(root) as s:
                    return s.retry(fid, cancel)
            self.run_task("重新解析附件", work)

    def edit_metadata(self):
        if not self.need_article():
            return
        a = self.store.article(self.article_id)
        dialog = TextDialog("修订元数据JSON · 日期可仅YYYY · 未报告填null · 作者身份须有明确依据", dumps(a["metadata"]), self, True)
        if dialog.exec():
            try:
                meta = strict_json(dialog.editor.toPlainText())
                self.store.save_metadata(self.article_id, meta, self.notes.toPlainText()); self.refresh()
            except Exception as exc:
                self.error(exc)

    def save_notes(self):
        if self.need_article():
            self.store.save_metadata(self.article_id, self.store.article(self.article_id)["metadata"], self.notes.toPlainText())
            self.status.setText("笔记已保存，修订已记入历史")

    def show_candidates(self):
        if self.need_article():
            TextDialog("元数据候选与字段来源 · 冲突保留供核验", dumps(self.store.article(self.article_id)["candidates"]), self).exec()

    def edit_section(self, name):
        if not self.need_article():
            return
        section = self.store.section(self.article_id, name)
        if not section:
            self.error("请先导入分析结果")
            return
        dialog = TextDialog("人工修订 · 保留修订历史 · JSON字段见格式说明", dumps({"content": section["content"], "evidence": section["evidence"]}), self, True)
        checkbox = QCheckBox("我已阅读原文并完成人工核验（引用未匹配时不允许勾选）")
        dialog.layout().insertWidget(1, checkbox)
        if dialog.exec():
            try:
                from jsonschema import validate
                value = strict_json(dialog.editor.toPlainText())
                if not isinstance(value, dict) or set(value) != {"content", "evidence"}:
                    raise ValueError("保留content和evidence两个字段")
                validate(value["content"], STORY if name == "story" else PROCESS)
                from .schema import EVIDENCE
                validate(value["evidence"], {"type": "array", "items": EVIDENCE})
                semantic_validate({name: value["content"]}, value["evidence"])
                checks = evidence_checks(self.store, self.article_id, value["evidence"])
                if checkbox.isChecked() and any(not c["matched"] for c in checks):
                    raise ValueError("仍有原文引用无法匹配，不能标记已核验；请修正引用")
                with self.store.db:
                    self.store.save_section(self.article_id, name, value["content"], value["evidence"], checks, "人工修订", checkbox.isChecked())
                self.refresh()
            except Exception as exc:
                self.error(exc)

    def copy_prompt(self):
        QApplication.clipboard().setText(PROMPT)
        self.status.setText("AI提示词已复制；附件文本、ID和模板请使用导出分析包")

    def export_analysis(self):
        if not self.need_article():
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出本文与全部配对SI分析包", str(self.store.root / "exchanges" / f"{self.article_id}.zip"), "分析包 (*.zip)")
        if path:
            root, aid = self.store.root, self.article_id
            def work(cancel, progress):
                with Store(root) as s:
                    return export_package(s, aid, path, cancel)
            self.run_task("导出全部文本（不截断）", work, lambda _: QMessageBox.information(self, "分析包已导出", path))

    def import_analysis(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择AI结果JSON（多篇可部分成功）", "", "结构化结果 (*.json)")
        if paths:
            self.preview_results(paths)

    def preview_results(self, paths):
        root = self.store.root
        def work(cancel, progress):
            results = []
            with Store(root) as s:
                for i, path in enumerate(paths):
                    if cancel():
                        break
                    progress(i, len(paths), "校验 " + Path(path).name)
                    try:
                        value = strict_json(Path(path).read_text(encoding="utf-8-sig"))
                        results.append({"file": Path(path).name, "value": value, "preview": preview(s, value)})
                    except Exception as exc:
                        results.append({"file": Path(path).name, "error": str(exc)})
            return results
        self.run_task("验证结构、文章ID、文件哈希及原文引用", work, self.confirm_results, False)

    def confirm_results(self, results):
        text = []
        for r in results:
            if "error" in r:
                text.append(r["file"] + "\n拒绝导入：" + r["error"])
            else:
                text.append(r["file"] + "\n" + dumps(r["preview"]))
        dialog = TextDialog("导入预览与差异 · 已核验内容仅存候选，不覆盖", "\n\n".join(text), self, True)
        dialog.editor.setReadOnly(True)
        choices = []
        for r in results:
            if "error" in r:
                continue
            names = []
            for name, change in r["preview"]["changes"].items():
                cb = QCheckBox(f"{r['file']} · {name}" + ("（人工已核验：保存新候选，不覆盖）" if change["protected"] else ""))
                cb.setChecked(True)
                dialog.layout().insertWidget(dialog.layout().count()-1, cb)
                names.append((name, cb))
            choices.append((r["value"], names))
        if dialog.exec():
            selected = [(v, [n for n, cb in names if cb.isChecked()]) for v, names in choices]
            root = self.store.root
            def work(cancel, progress):
                report = [{"file": r["file"], "status": "error", "reason": r["error"]} for r in results if "error" in r]
                with Store(root) as s:
                    for i, (v, names) in enumerate(selected):
                        if cancel():
                            break
                        if not names:
                            continue
                        progress(i, len(selected), "保存导回结果")
                        try:
                            report.append(apply_result(s, v, names))
                        except Exception as exc:
                            report.append({"status": "error", "reason": str(exc)})
                    atomic_json(root / "results" / f"result-report-{uid()}.json", report)
                return report
            self.run_task("应用选中分析段", work, lambda r: TextDialog("导入报告", dumps(r), self).exec())

    def export_excel(self):
        ids = self.selected_ids()
        if not ids:
            ids = [a["article_id"] for a in self.store.list_articles(self.search.text(), {self.filter_field.currentData(): self.filter_value.text()})]
        if not ids:
            self.error("没有可导出的文献"); return
        path, _ = QFileDialog.getSaveFileName(self, f"导出比较表（{len(ids)}篇，保留样品和测量条件）", "钙钛矿文献比较.xlsx", "Excel (*.xlsx)")
        if path:
            root = self.store.root
            def work(cancel, progress):
                with Store(root) as s:
                    excel_export(s, ids, path, cancel)
                return path
            self.run_task("导出关联工作表", work, lambda p: QMessageBox.information(self, "Excel已导出", p), False)

    def export_markdown(self):
        if self.need_article():
            path, _ = QFileDialog.getSaveFileName(self, "导出阅读卡", "阅读卡.md", "Markdown (*.md)")
            if path:
                Path(path).write_text(markdown(self.store, self.article_id), encoding="utf-8")
                self.status.setText("Markdown阅读卡已导出")

    def tree_evidence(self, item, _):
        ids = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(ids, list) and ids:
            self.jump_evidence(ids[0])

    def jump_evidence(self, eid):
        for i in range(self.evidence.topLevelItemCount()):
            item = self.evidence.topLevelItem(i)
            if item.text(0) == eid:
                evidence = item.data(0, Qt.ItemDataRole.UserRole)
                self.open_file(evidence["file_id"], evidence)
                return
        self.error("当前文章未找到证据ID")

    def open_attachment(self):
        fid = self.chosen_file()
        if fid:
            self.open_file(fid)

    def open_file(self, fid, evidence=None):
        if self.worker:
            return
        self.preview_file, self.preview_evidence = fid, evidence
        self.tabs.setCurrentWidget(self.viewer_tab)
        try:
            self.reader.open_file(fid,evidence)
        except Exception as exc:
            self.error(exc)

    def render_page(self):
        self.reader.render_pdf()

    def toggle_reader_mode(self):
        self._reading_mode = not self._reading_mode
        if self._reading_mode:
            self._reading_sizes = self.main_split.sizes()
        self.sidebar.setVisible(not self._reading_mode)
        self.center_panel.setVisible(not self._reading_mode)
        self.reader.focus_button.setText("退出阅读模式" if self._reading_mode else "阅读模式")
        if not self._reading_mode:
            self.main_split.setSizes(self._reading_sizes)

    def api_settings(self):
        config = strict_json((self.store.root / "config.json").read_text(encoding="utf-8"))
        dialog = QDialog(self)
        dialog.setWindowTitle("可选联网配置 · API Key仅保留在本次会话内存")
        form = QFormLayout(dialog)
        enabled = QCheckBox("主动开启联网功能（启动仍不联网）")
        enabled.setChecked(config.get("api_enabled", False))
        base = QLineEdit(config.get("base_url", ""))
        model = QLineEdit(config.get("model", ""))
        key = QLineEdit(getattr(self, "api_key", "")); key.setEchoMode(QLineEdit.EchoMode.Password)
        timeout = QSpinBox(); timeout.setRange(5, 300); timeout.setValue(config.get("timeout", 60))
        form.addRow(enabled)
        for label, control in [("API基础地址（含/v1）", base), ("模型名", model), ("API Key（不写入磁盘）", key), ("超时秒数", timeout)]:
            form.addRow(label, control)
        form.addRow(QLabel("开启后也仅在点击Crossref或API分析时发送数据。\nAPI分析会发送本文及全部配对SI提取文本；ChatGPT订阅不含API权限。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec():
            config = {"api_enabled": enabled.isChecked(), "base_url": base.text().strip(), "model": model.text().strip(), "timeout": timeout.value()}
            atomic_json(self.store.root / "config.json", config)
            self.api_key = key.text()
        return config

    def lookup_crossref(self):
        if not self.need_article():
            return
        config = strict_json((self.store.root / "config.json").read_text(encoding="utf-8"))
        if not config.get("api_enabled"):
            self.error("联网默认关闭，请在“更多 → 联网配置”主动开启")
            return
        aid, root = self.article_id, self.store.root
        doi = self.store.article(aid)["metadata"].get("doi")
        if not doi:
            self.error("请先修订本篇DOI，勿使用参考文献DOI"); return
        def work(cancel, progress):
            value = crossref(doi, config["timeout"])
            if cancel():
                raise InterruptedError("已取消")
            with Store(root) as s:
                s.add_candidates(aid, value, "Crossref DOI主动查询")
            return value
        self.run_task("主动查询Crossref（只追加候选）", work, lambda r: TextDialog("Crossref候选 · 请核验后修订元数据", dumps(r), self).exec())

    def api_analyze(self):
        if not self.need_article():
            return
        config = strict_json((self.store.root / "config.json").read_text(encoding="utf-8"))
        if not config.get("api_enabled") or not getattr(self, "api_key", ""):
            config = self.api_settings()
        if not config.get("api_enabled") or not getattr(self, "api_key", ""):
            return
        aid, root, key = self.article_id, self.store.root, self.api_key
        def work(cancel, progress):
            with Store(root) as s:
                return analyze(s, aid, config, key, cancel, progress)
        def finished(result):
            TextDialog("API用量（服务商可获得时）", dumps(result.get("usage", [])), self).exec()
            self.preview_results([result["result_path"]])
        self.run_task("用户主动启动API分块分析", work, finished)

    def more_menu(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        for label, callback in [("切换数据目录…", self.change_data_directory), ("联网配置", self.api_settings), ("完整备份", self.backup_data), ("恢复到空目录", self.restore_data), ("修订历史 / 待采用分析候选", self.show_history), ("使用说明", self.show_help)]:
            menu.addAction(label, callback)
        menu.exec(self.cursor().pos())

    def change_data_directory(self):
        if self.worker or self.reader.export_worker:
            self.error("请等待当前任务完成后切换数据目录"); return
        path=QFileDialog.getExistingDirectory(self,"切换数据目录（不会移动或删除当前数据）",str(self.store.root.parent))
        if path and Path(path).resolve()!=self.store.root:
            self.directoryRequested.emit(path)

    def backup_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "备份到数据目录外（不含Key）", "钙钛矿文献备份.zip", "ZIP (*.zip)")
        if path:
            root = self.store.root
            def work(cancel, progress):
                with Store(root) as s:
                    s.backup(path, cancel)
                return path
            self.run_task("完整备份数据库、附件和结果", work, lambda p: QMessageBox.information(self, "备份完成", p), False)

    def restore_data(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择完整备份", "", "ZIP (*.zip)")
        if not source:
            return
        destination = QFileDialog.getExistingDirectory(self, "选择空的恢复目录（不覆盖原数据）")
        if destination:
            self.run_task("恢复并校验全部附件哈希", lambda c, p: restore(source, destination), lambda _: QMessageBox.information(self, "恢复完成", "重启程序并选择恢复目录即可使用"), False)

    def show_history(self):
        if self.need_article():
            history = self.store.rows("SELECT * FROM history WHERE article_id=? ORDER BY created DESC", (self.article_id,))
            candidates = self.store.rows("SELECT payload,report,created FROM imports WHERE article_id=? ORDER BY created DESC", (self.article_id,))
            TextDialog("人工修订历史与分析候选 · 已核验段可复制候选到人工修订窗口采用", dumps({"history": history, "analysis_candidates": candidates}), self).exec()

    def show_help(self):
        TextDialog("钙钛矿文献助手 · 第一版", "选择独立数据目录。导入默认复制原件，哈希去重。\n在附件页确认正文角色；选择SI，点击关联并指定正文文章，允许多个SI及不同版本。\n确认全部附件后导出分析包，阅读包内prompt与schema。将AI完成的JSON导回，检查差异与引用。\n人工修订/核验保留历史；已核验内容不会被AI重新分析覆盖，新结果保留候选。\n工艺和性能按样品/测量条件展示，空值为未报告。多选文章导出Excel作比较。\n默认离线。Key仅会话内存。扫描件不OCR，DOC不解析，图片/复杂表格需对照原件。\n数据目录可整体复制、搬移；请勿同时编辑同一目录。完整备份和恢复在“更多”。\n元数据和复杂结构第一版使用带校验的JSON编辑窗口。更详细说明见随附README。", self).exec()

    def closeEvent(self, event):
        if self.worker:
            self.cancel_task()
            self.error("任务正在取消，请等待结束后关闭；已完成块会保留")
            event.ignore(); return
        if self.reader.active_worker or self.reader.export_worker:
            self.reader.stop()
            self.status.setText("正在结束页面渲染…")
            QTimer.singleShot(100,self.close)
            event.ignore(); return
        self.reader.stop()
        self.store.close()
        self.lock.unlock()
        event.accept()
