import html
import json
import os
import threading
import copy
import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QLockFile, QSize
from PySide6.QtGui import QPixmap, QImage, QIcon, QAction, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView, QLineEdit, QComboBox,
    QPushButton, QLabel, QTabWidget, QTextBrowser, QPlainTextEdit, QFileDialog, QMessageBox, QDialog,
    QDialogButtonBox, QFormLayout, QCheckBox, QSpinBox, QScrollArea, QProgressBar, QGroupBox, QHeaderView, QSizePolicy, QToolBar, QListWidget, QListWidgetItem, QInputDialog)

from .exchange import preview, apply_result, strict_json, export_package, evidence_checks, semantic_validate
from .exporting import excel_export, markdown
from .schema import META_FIELDS, STORY_FIELDS, TAGS, PROMPT, STORY, PROCESS
from .store import Store, dumps, atomic_json, restore, uid, UNFILED
from .categories import ArticleTable,CategoryTree
from .network import crossref, analyze
from .reader import ReaderPanel
from .editing import InlineDelegate,EDIT_ROLE,bind,text_value,parse_value,save_content_edit
from .ui_icons import icon,svg_pixmap

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
        self.category_id=None
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
        outer.setContentsMargins(6,3,6,3); outer.setSpacing(3)
        self.banner = banner = QLabel("钙钛矿文献助手")
        banner.setStyleSheet("font-size:18px;font-weight:600;color:#214d62;padding:2px")
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(svg_pixmap((Path(__file__).resolve().parent.parent/'assets/logo-book-octahedron.svg').read_bytes(),32,max(2,self.devicePixelRatioF())))
        brand.addWidget(logo)
        brand.addWidget(banner)
        self.path_label = QLabel("数据目录：" + str(self.store.root) + "   ｜请勿同时编辑同一数据目录")
        self.path_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.path_label.setToolTip(self.path_label.text())
        self.path_label.setStyleSheet("color:#637382;font-size:11px")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        brand.addWidget(self.path_label,1); outer.addLayout(brand)
        self.toolbar=QToolBar(); self.toolbar.setMovable(False); self.toolbar.setIconSize(QSize(22,22)); self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.toolbar.setStyleSheet('QToolButton{padding:4px;border:1px solid transparent;border-radius:4px} QToolButton:hover{background:#dceaf1;border-color:#adc5d2}')
        outer.addWidget(self.toolbar)
        for name,label,callback in [('import','导入文件',self.import_files),('folder','导入文件夹',self.import_folder),('attach','关联附件',self.pair_file),('export','导出分析包',self.export_analysis),('result','导回结果',self.import_analysis),('prompt','复制AI提示词',self.copy_prompt),('api','API分析',self.api_analyze),('excel','比较 / Excel',self.export_excel),('tags','自定义标签',self.manage_tags),('switch','切换数据目录',self.change_data_directory),('settings','联网配置',self.api_settings),('backup','完整备份',self.backup_data),('restore','恢复到空目录',self.restore_data),('history','修订历史 / 待采用分析候选',self.show_history),('help','使用说明',self.show_help)]:
            action=self.toolbar.addAction(icon(name),label,callback); action.setToolTip(label); self.busy_controls.append(action)
            self.toolbar.widgetForAction(action).setAccessibleName(label)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split = split
        self._reading_mode = False
        outer.addWidget(split, 1)
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderLabel("专题 / 标签 / 处理状态")
        self.sidebar.setMaximumWidth(250)
        self.sidebar.setMinimumWidth(100)
        self.sidebar.setUniformRowHeights(True); self.sidebar.setIndentation(12)
        self.refresh_sidebar()
        self.sidebar.itemClicked.connect(self.sidebar_filter)
        self.sidebar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sidebar.customContextMenuRequested.connect(lambda _:self.manage_tags())
        self.sidebar_panel=QWidget(); side=QVBoxLayout(self.sidebar_panel); side.setContentsMargins(0,0,0,0); side.setSpacing(3)
        self.sidebar_panel.setMaximumWidth(280); self.sidebar_panel.setMinimumWidth(130)
        categorybar=QHBoxLayout(); categorybar.addWidget(QLabel('手动分类'),1)
        for label,tip,callback in [('+','创建类别',self.create_category),('✎','重命名类别',self.rename_category),('−','删除类别，保留文献',self.delete_category)]:
            button=QPushButton(label); button.setFixedWidth(24); button.setToolTip(tip); button.setAccessibleName(tip); button.clicked.connect(callback); categorybar.addWidget(button); self.busy_controls.append(button)
        side.addLayout(categorybar)
        self.category_tree=CategoryTree(self.store.root); self.category_tree.setHeaderHidden(True); self.category_tree.setIndentation(10)
        self.category_tree.setMinimumHeight(100); self.category_tree.setMaximumHeight(230)
        self.category_tree.setToolTip('将文献清单中的文献按住鼠标拖入类别；Ctrl/Shift可多选。一篇文献可加入多个类别。右键可移出、改名或删除类别。')
        self.category_tree.itemClicked.connect(self.category_clicked); self.category_tree.articlesDropped.connect(self.classify_articles)
        self.category_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.category_tree.customContextMenuRequested.connect(self.category_menu)
        side.addWidget(self.category_tree,1); side.addWidget(self.sidebar,2); split.addWidget(self.sidebar_panel)
        self.refresh_categories()
        center = QWidget()
        self.center_panel = center
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(3); center.setMinimumWidth(240)
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
        self.list_details.toggled.connect(lambda visible:[self.table.setColumnHidden(col,not visible) for col in [1,2,3,4]])
        countbar.addWidget(self.list_details); cl.addLayout(countbar)
        self.table = ArticleTable(self.store.root)
        self.table.setHorizontalHeaderLabels(["文献标题", "期刊 / 年份", "SI数", "处理状态","通讯作者"])
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
        self.table.setColumnWidth(4,140)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(30)
        for col in [1,2,3,4]: self.table.setColumnHidden(col,True)
        self.table.itemSelectionChanged.connect(self.select_article)
        cl.addWidget(self.table, 1)
        help_label=QLabel("拖到左侧分类 · Ctrl/Shift多选")
        help_label.setToolTip("配对建议仅供确认，同DOI不同版本均保留；悬停单元格可查看完整标题和状态")
        cl.addWidget(help_label)
        split.addWidget(center)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setStretchFactor(2,3)
        split.setSizes([170,380,1000])
        self.meta_tab = QWidget()
        ml = QVBoxLayout(self.meta_tab)
        self.meta = QTableWidget(0,2); self.meta.setHorizontalHeaderLabels(['文献信息','内容（双击编辑）'])
        self.meta.verticalHeader().hide(); self.meta.setColumnWidth(0,175); self.meta.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.meta.setWordWrap(True); self.meta.setItemDelegate(InlineDelegate(self.meta)); self.meta.itemChanged.connect(self.inline_metadata_changed)
        self.meta.itemDoubleClicked.connect(lambda item:self.meta.editItem(item) if item.data(EDIT_ROLE) else None)
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
        self.story = QTreeWidget(); self.story.setHeaderLabels(['核心故事（双击直接编辑）','声明类别','证据'])
        self.story.setColumnWidth(0,450); self.story.setColumnWidth(1,120); self.story.setWordWrap(True)
        self.story.setItemDelegate(InlineDelegate(self.story)); self.story.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.story.itemChanged.connect(lambda item,col:self.inline_section_changed('story',item,col)); self.story.itemDoubleClicked.connect(self.story_double_clicked)
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
        self.process.setItemDelegate(InlineDelegate(self.process)); self.process.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.process.itemDoubleClicked.connect(lambda item,col:self.process_double_clicked(item,col,'samples'))
        self.process.itemChanged.connect(lambda item,col:self.inline_section_changed('process',item,col))
        pl.addWidget(self.process)
        self.button("人工修订 / 核验工艺性能", lambda: self.edit_section("process"), pl)
        self.tabs.addTab(self.process_tab, "工艺")
        self.performance_tab = QWidget()
        pfl = QVBoxLayout(self.performance_tab)
        pfl.addWidget(QLabel("按样品和测量条件展示。稳定性条件不同不能直接统一排名。"))
        self.performance = QTreeWidget()
        self.performance.setHeaderLabels(["样品 / 测量类型 / 参数", "原文值", "标准化值", "测量条件 / 冲突", "证据"])
        self.performance.setColumnWidth(0, 230)
        self.performance.setItemDelegate(InlineDelegate(self.performance)); self.performance.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.performance.itemDoubleClicked.connect(lambda item,col:self.process_double_clicked(item,col,'performance'))
        self.performance.itemChanged.connect(lambda item,col:self.inline_section_changed('process',item,col))
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
        for label, callback in [("选择附件关联…", self.pair_file), ("查看", self.open_attachment), ("修改角色", self.edit_file_role), ("解除关联", self.detach_file), ("重试解析", self.retry_file)]:
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

    @property
    def preview_image(self): return self.reader.canvas

    def refresh_categories(self):
        self.category_tree.blockSignals(True); self.category_tree.clear()
        entries=[(None,'全部文献'),(UNFILED,'未分类')]+[(c['category_id'],f"{c['name']}  ({c['article_count']})") for c in self.store.categories()]
        for cid,name in entries:
            item=QTreeWidgetItem([name]); item.setData(0,Qt.ItemDataRole.UserRole,cid); item.setToolTip(0,name)
            item.setIcon(0,icon('folder')); self.category_tree.addTopLevelItem(item)
            if cid==self.category_id: self.category_tree.setCurrentItem(item)
        self.category_tree.blockSignals(False)

    def category_clicked(self,item,_=0):
        self.category_id=item.data(0,Qt.ItemDataRole.UserRole); self.refresh()

    def current_category(self):
        item=self.category_tree.currentItem(); cid=item.data(0,Qt.ItemDataRole.UserRole) if item else None
        return cid if cid and cid!=UNFILED else None

    def create_category(self):
        name,ok=QInputDialog.getText(self,'创建类别','类别名称：')
        if ok:
            try: self.store.create_category(name); self.refresh_categories()
            except ValueError as exc: self.error(str(exc))

    def rename_category(self):
        cid=self.current_category()
        if not cid: return
        name,ok=QInputDialog.getText(self,'重命名类别','类别名称：',text=self.store.category(cid)['name'])
        if ok:
            try: self.store.rename_category(cid,name); self.refresh_categories()
            except ValueError as exc: self.error(str(exc))

    def delete_category(self):
        cid=self.current_category()
        if not cid: return
        self.store.delete_category(cid)
        if self.category_id==cid: self.category_id=None
        self.refresh(); self.status.setText('已删除类别，文献和附件完整保留')

    def classify_articles(self,cid,ids):
        if self.worker: self.status.setText('当前任务完成后可继续分类'); return
        try:
            count=self.store.assign_category(ids,cid); name=self.store.category(cid)['name']
            self.refresh_categories(); self.status.setText(f'已将 {count} 篇文献加入“{name}”；重复归类自动跳过')
        except ValueError as exc: self.error(str(exc))

    def remove_from_category(self):
        cid=self.current_category(); ids=self.selected_ids()
        if cid and ids:
            self.store.remove_category(ids,cid); self.refresh(); self.status.setText('已移出类别，文献和附件完整保留')

    def category_menu(self,point):
        from PySide6.QtWidgets import QMenu
        item=self.category_tree.itemAt(point)
        if item: self.category_tree.setCurrentItem(item)
        menu=QMenu(self); menu.addAction('创建类别',self.create_category)
        if self.current_category():
            menu.addAction('将所选文献加入此类别',lambda:self.classify_articles(self.current_category(),self.selected_ids()))
            menu.addAction('将所选文献移出此类别',self.remove_from_category)
            menu.addSeparator(); menu.addAction('重命名类别',self.rename_category); menu.addAction('删除类别（保留文献）',self.delete_category)
        menu.exec(self.category_tree.viewport().mapToGlobal(point))

    def refresh_sidebar(self):
        self.sidebar.clear()
        all_item=QTreeWidgetItem(['全部文献']); all_item.setData(0,Qt.ItemDataRole.UserRole,('','')); self.sidebar.addTopLevelItem(all_item)
        for title,field,values in [('处理状态','state',['解析需关注','故事待分析','工艺待分析','引用待核验','人工待核验','人工已核验']),('专题标签（右键管理）','tags',self.store.tag_names())]:
            parent=QTreeWidgetItem([title]); self.sidebar.addTopLevelItem(parent)
            for value in values:
                item=QTreeWidgetItem([value]); item.setData(0,Qt.ItemDataRole.UserRole,(field,value)); item.setToolTip(0,value); parent.addChild(item)
            parent.setExpanded(True)

    def manage_tags(self):
        if self.worker or self.reader.export_worker:
            self.error('请等待当前任务完成后编辑标签'); return
        aid=self.article_id; current=self.store.article(aid)['metadata']['tags'] if aid else []
        original=self.store.tag_names()
        dialog=QDialog(self); dialog.setWindowTitle('自定义专题标签'); dialog.resize(520,540)
        layout=QVBoxLayout(dialog); layout.addWidget(QLabel('双击标签名称可修改；勾选表示应用到当前文献。删除或重命名会同步已有文献标签。'))
        items=QListWidget(); items.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed); layout.addWidget(items)
        def add(name='',old=None):
            if not name:
                name,ok=QInputDialog.getText(dialog,'新增标签','标签名称')
                if not ok or not name.strip(): return
            item=QListWidgetItem(name.strip()); item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable|Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole,old); item.setCheckState(Qt.CheckState.Checked if name in current else Qt.CheckState.Unchecked); items.addItem(item)
        for name in original: add(name,name)
        row=QHBoxLayout()
        self.button('新增',lambda:add(),row,False)
        self.button('重命名',lambda:items.editItem(items.currentItem()) if items.currentItem() else None,row,False)
        self.button('删除',lambda:items.takeItem(items.currentRow()) if items.currentRow()>=0 else None,row,False)
        layout.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if not dialog.exec(): return
        names=[items.item(i).text().strip() for i in range(items.count())]
        renames={items.item(i).data(Qt.ItemDataRole.UserRole):names[i] for i in range(items.count()) if items.item(i).data(Qt.ItemDataRole.UserRole) and items.item(i).data(Qt.ItemDataRole.UserRole)!=names[i]}
        retained={items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(items.count())}
        try:
            self.store.manage_tags(names,renames,set(original)-retained)
            if aid:
                article=self.store.article(aid); article['metadata']['tags']=list(dict.fromkeys(names[i] for i in range(items.count()) if items.item(i).checkState()==Qt.CheckState.Checked))
                self.store.save_metadata(aid,article['metadata'],article['notes'])
            if self.filter_field.currentData()=='tags':
                value=self.filter_value.text()
                if value in renames: self.filter_value.setText(renames[value])
                elif value in set(original)-retained: self.filter_value.clear()
            self.refresh_sidebar(); self.refresh()
        except Exception as exc: self.error(exc)

    def inline_metadata_changed(self,item):
        binding=item.data(EDIT_ROLE)
        if not binding: return
        aid=binding['article_id']; article=self.store.article(aid); key=binding['path'][0]
        try:
            metadata=copy.deepcopy(article['metadata']); metadata[key]=parse_value(item.text(),metadata.get(key),binding.get('kind'))
            self.store.save_metadata(aid,metadata,self.notes.toPlainText() if aid==self.article_id else article['notes'])
            if key=='tags': self.refresh_sidebar()
            QTimer.singleShot(0,self.refresh)
            self.status.setText('文献信息已保存，修订历史保留')
        except Exception as exc:
            self.meta.blockSignals(True); item.setText(text_value(article['metadata'].get(key))); self.meta.blockSignals(False); self.error(exc)

    def populate_story(self):
        self.story.blockSignals(True); self.story.clear()
        section=self.store.section(self.article_id,'story')
        self.story.headerItem().setText(0,'核心故事（双击编辑 · '+('人工已核验' if section and section['human_verified'] else '人工待核验')+'）')
        for field,label in STORY_FIELDS.items():
            root=QTreeWidgetItem([label]); self.story.addTopLevelItem(root)
            claims=section['content'][field] if section else []
            for i,claim in enumerate(claims or [{'text':'双击填写','kind':'ai_inference','evidence_ids':[]}]):
                item=QTreeWidgetItem([claim['text'],{'author_claim':'作者主张','direct_result':'直接结果','ai_inference':'推测/建议'}[claim['kind']],', '.join(claim['evidence_ids'])])
                bind(item,0,self.article_id,[field,i,'text'],True,'string')
                if claims:
                    bind(item,1,self.article_id,[field,i,'kind'],False,'string')
                    binding=item.data(1,EDIT_ROLE); binding['choices']=[('作者主张','author_claim'),('直接结果','direct_result'),('推测/建议','ai_inference')]; binding['value']=claim['kind']; item.setData(1,EDIT_ROLE,binding)
                item.setData(2,Qt.ItemDataRole.UserRole,claim['evidence_ids']); item.setToolTip(0,claim['text'])
                root.addChild(item)
        self.story.expandAll(); self.story.blockSignals(False)

    def story_double_clicked(self,item,column):
        if column==2:
            ids=item.data(2,Qt.ItemDataRole.UserRole)
            if ids: self.jump_evidence(ids[0])
        elif item.data(column,EDIT_ROLE): self.story.editItem(item,column)

    def inline_section_changed(self,name,item,column):
        binding=item.data(column,EDIT_ROLE)
        if not binding: return
        try:
            save_content_edit(self.store,binding['article_id'],name,binding['path'],item.text(column),binding.get('kind'))
            self.status.setText('内容已保存并保留历史；人工编辑后需重新核验结论')
        except Exception as exc: self.error(exc)
        QTimer.singleShot(0,self.populate_story if name=='story' else self.populate_process)

    def process_double_clicked(self,item,column,kind):
        action=item.data(0,int(Qt.ItemDataRole.UserRole)+41)
        if action: self.add_process_record(*action)
        elif column==4: self.tree_evidence(item,column)
        elif item.data(column,EDIT_ROLE): (self.process if kind=='samples' else self.performance).editItem(item,column)

    def add_process_record(self,kind,path=None):
        if not self.article_id: return
        section=self.store.section(self.article_id,'process')
        content=copy.deepcopy(section['content']) if section else {'samples':[],'performance':[]}
        node=content
        for key in path or []: node=node[key]
        if kind=='sample': content['samples'].append({'sample_id':uid(),'label':'新样品（双击修改）','group':'other','steps':[]})
        elif kind=='step': node.append({'order':len(node),'category':'工艺步骤','description':'双击填写操作','parameters':[],'evidence_ids':[]})
        elif kind=='parameter': node.append({'name':'新参数','original_value':None,'original_unit':None,'normalized_value':None,'normalized_unit':None,'basis':None,'conversion_note':None,'conflict_group':None,'evidence_ids':[]})
        elif kind=='performance':
            if not content['samples']: content['samples'].append({'sample_id':uid(),'label':'新样品','group':'other','steps':[]})
            content['performance'].append({'record_id':uid(),'sample_id':content['samples'][0]['sample_id'],'measurement_kind':'新测量','conditions':{},'values':[],'evidence_ids':[]})
        elif kind=='condition':
            name,ok=QInputDialog.getText(self,'添加测试条件','条件名称（例如温度、湿度、光照）')
            if not ok or not name.strip(): return
            node[name.strip()]=None
        evidence=section['evidence'] if section else []
        with self.store.db: self.store.save_section(self.article_id,'process',content,evidence,evidence_checks(self.store,self.article_id,evidence),'人工添加工艺/性能',False)
        self.populate_process()

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
        self.refresh_categories()
        previous = self.article_id
        filters = {self.filter_field.currentData(): self.filter_value.text()}
        rows = self.store.list_articles(self.search.text(), filters,self.category_id)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        selected_row = None
        for i, a in enumerate(rows):
            m = a["metadata"]
            vals = [m.get("title_zh") or m.get("title") or "未命名文献", f"{m.get('journal') or '未报告'} / {m.get('year') or '未报告'}", str(sum(f["role"] == "si" for f in self.store.files(a["article_id"]))), a["state"],'; '.join(m.get('corresponding_authors') or []) or '未报告']
            for j, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setToolTip("\n".join([vals[0],vals[1],"SI数量："+vals[2],vals[3],'通讯作者：'+vals[4]]) if j==0 else value)
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
            self.meta.blockSignals(True); self.meta.setRowCount(0); self.meta.blockSignals(False)
            self.story.clear()
            self.process.clear()
            self.performance.clear()
            self.evidence.clear()
            self.attachments.clear()
            self.notes.clear()
            self.reader.set_files([]); self.reader.clear_file("请选择或导入文献")
            self.preview_file=self.preview_evidence=None

    def clear_filters(self):
        self.category_id=None
        self.search.clear()
        self.filter_value.clear()
        self.refresh()

    def sidebar_filter(self, item, _):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            field, value = data
            if not field: self.category_id=None
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
        self.meta.blockSignals(True); self.meta.setRowCount(len(META_FIELDS))
        for row,(k, label) in enumerate(META_FIELDS.items()):
            value = m.get(k)
            labelitem=QTableWidgetItem(label); labelitem.setFlags(labelitem.flags() & ~Qt.ItemFlag.ItemIsEditable)
            cell=QTableWidgetItem(text_value(value)); cell.setToolTip(text_value(value))
            cell.setData(EDIT_ROLE,{'article_id':self.article_id,'path':[k],'multiline':k in {'title','title_zh','affiliations'},'kind':'array' if isinstance(value,list) else 'boolean' if k=='si_complete' else None})
            self.meta.setItem(row,0,labelitem); self.meta.setItem(row,1,cell)
            self.meta.setRowHeight(row,32)
        self.meta.blockSignals(False)
        self.notes.setPlainText(a["notes"])
        self.populate_story()
        self.evidence.clear()
        for name in ["story", "process"]:
            section = self.store.section(self.article_id, name)
            if not section:
                continue
            verify_label = "人工已核验" if section["human_verified"] else "人工待核验"
            checks = {c["evidence_id"]: c for c in section["checks"]}
            for e in section["evidence"]:
                item = QTreeWidgetItem([e["evidence_id"], f"{e['file_id'][:8]} / {e['loc_id']} / {e['figure'] or '未报告'}", e["quote"], ("原文匹配" if checks.get(e["evidence_id"], {}).get("matched") else "引用待核验") + " / " + verify_label])
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                self.evidence.addTopLevelItem(item)
        self.populate_process()
        self.attachments.clear()
        for f in self.store.files(self.article_id):
            item = QTreeWidgetItem([f["name"], ROLE_NAMES[f["role"]], STATE_NAMES.get(f["status"], f["status"]), f["pairing"] + "；" + f["reason"]])
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

    def show_article_original(self,reveal=False):
        if not self.article_id or self.worker: return
        files=self.store.files(self.article_id)
        priority={"main":0,"version":1,"preprint":2,"si":3,"other":4}
        candidates=sorted((f for f in files if Path(f['path']).suffix.lower() in {'.pdf','.docx'} and f['status'] in {'ready','partial','no_text'}),key=lambda f:priority.get(f['role'],5))
        if reveal: self.tabs.setCurrentWidget(self.viewer_tab)
        if not candidates:
            self.preview_file=self.preview_evidence=None
            reason="；".join(f["name"]+"："+f["reason"] for f in files)
            self.reader.clear_file(reason or "当前文献没有附件，请在附件与配对中关联文件")
            return
        fid=candidates[0]['file_id']
        if self.reader.file_id!=fid or self.reader.evidence is not None or (self.reader.kind=='pdf' and self.reader.canvas.data is None and not self.reader.active_worker):
            self.open_file(fid,reveal=reveal)

    def populate_process(self):
        from .editing import populate_process
        populate_process(self)

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
        self.run_task("扫描并导入附件", work, lambda r: TextDialog("导入报告 · 可直接导出分析包", dumps(r), self).exec())

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
        dialog.setWindowTitle("修改附件角色")
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
            if prior["role"]!=role.currentData():
                self.store.associate(fid, self.article_id, role.currentData())
            self.refresh(); self.tabs.setCurrentWidget(self.attach_tab)

    def detach_file(self):
        fid = self.chosen_file()
        if fid:
            self.article_id = self.store.detach(fid); self.refresh()

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
            ids = [a["article_id"] for a in self.store.list_articles(self.search.text(), {self.filter_field.currentData(): self.filter_value.text()},self.category_id)]
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

    def open_file(self, fid, evidence=None, reveal=True):
        if self.worker:
            return
        self.preview_file, self.preview_evidence = fid, evidence
        if reveal: self.tabs.setCurrentWidget(self.viewer_tab)
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
        self.sidebar_panel.setVisible(not self._reading_mode)
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
            config.update({"api_enabled": enabled.isChecked(), "base_url": base.text().strip(), "model": model.text().strip(), "timeout": timeout.value()})
            atomic_json(self.store.root / "config.json", config)
            self.api_key = key.text()
        return config

    def lookup_crossref(self):
        if not self.need_article():
            return
        config = strict_json((self.store.root / "config.json").read_text(encoding="utf-8"))
        if not config.get("api_enabled"):
            self.error("联网默认关闭，请点顶部“联网配置”图标主动开启")
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
        text="""首次选择数据目录后自动记忆。顶部图标悬停显示功能名称，包含导入、关联附件、分析包交换、标签管理、换库、备份恢复等。
导入文件后可直接导出分析包，无需确认角色或配对。需要SI时，先选正文文献，再点关联附件浏览电脑文件。
左侧手动分类的+按钮可创建类别；将清单文献拖入类别，Ctrl/Shift可多选。一篇文献可归入多个类别。右键可移出、改名或删除类别，文献与附件保留。
自定义标签可新增、重命名、删除并勾选应用到当前文献；左侧标签上右键也可打开管理。
文献信息、核心故事、工艺与性能字段可直接双击编辑。单行Enter保存，多行Ctrl+Enter保存，Esc取消，离开编辑框也保存；修订历史保留。编辑后的科学结论需重新核验。
原文打开后后台预加载全部页面，显示进度并可暂停、继续。加载完成后滚动、跳页、缩放均复用图像。全文图像预算512MiB，超长文档会明确提示分辨率调整。页码下箭头向后、上箭头向前。支持单字母选择，查找句子、高亮与删除高亮常驻。
点击原文高亮或列表记录后按Delete删除；批注列双击直接编辑，位置/原文列双击跳转。删除键在文字输入框内正常删字。
切换左侧文献保留当前页签。详情列包含通讯作者，完整名单可悬停查看。
默认离线，不内置OCR；DOC暂不解析。PDF另存/写回批注在更多阅读操作中。所有数据在所选目录，附件可随目录搬移。
更详细说明见随附README。"""
        TextDialog("钙钛矿文献助手 · 使用说明",text,self).exec()

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
        self.reader.pdf_view.clear()
        self.store.close()
        self.lock.unlock()
        event.accept()
