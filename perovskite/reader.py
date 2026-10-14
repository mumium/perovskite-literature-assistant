"""PDF text selection and portable annotations, kept outside the original files."""
import json
import math
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QImage, QFont, QTextCursor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSpinBox, QPushButton,
    QScrollArea, QPlainTextEdit, QComboBox, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QApplication, QMenu, QTextEdit, QDialog, QDialogButtonBox, QFormLayout, QSplitter, QFileDialog, QMessageBox, QSizePolicy)

from .extract import digest, PDF_LOCK

COLORS = {"yellow": ("黄色", "#ffd84d"), "green": ("绿色", "#7ddc91"), "blue": ("蓝色", "#77caff"), "pink": ("粉色", "#ff9bc4")}

class StatusLabel(QLabel):
    def setText(self,text):
        super().setText(text)
        self.setToolTip(text)

def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2

def transform_rect(rect,matrix):
    a,b,c,d,e,f=matrix
    points=[(x*a+y*c+e,x*b+y*d+f) for x,y in [(rect[0],rect[1]),(rect[2],rect[1]),(rect[2],rect[3]),(rect[0],rect[3])]]
    return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]

def utf16_to_index(text, offset):
    return len(text.encode("utf-16-le")[:offset*2].decode("utf-16-le", errors="ignore"))

def docx_display(locations):
    parts, spans, offset = [], [], 0
    for loc in locations:
        heading = f"[{loc['loc_id']}] {loc.get('section', '')}\n"
        parts.append(heading)
        offset += utf16_length(heading)
        spans.append({"loc_id": loc["loc_id"], "start": offset, "end": offset+utf16_length(loc["text"]), "text": loc["text"]})
        parts.append(loc["text"] + "\n\n")
        offset += utf16_length(loc["text"] + "\n\n")
    return "".join(parts), spans

def docx_selection(spans, start, end):
    anchors, texts = [], []
    for span in spans:
        lo, hi = max(start, span["start"]), min(end, span["end"])
        if lo < hi:
            a = utf16_to_index(span["text"], lo-span["start"])
            b = utf16_to_index(span["text"], hi-span["start"])
            anchors.append({"loc_id": span["loc_id"], "start": a, "end": b})
            texts.append(span["text"][a:b])
    return {"text": "\n".join(texts), "anchors": anchors}

def load_pdf_page(path, page_index, zoom=100, fit_width=None, evidence=None, query="", expected_hash=None, pixel_ratio=2, cancel=lambda:False):
    import fitz
    if expected_hash and digest(path, cancel) != expected_hash:
        raise ValueError("附件哈希已变化，请从原件重新导入；未把高亮关联到改变后的文件")
    with PDF_LOCK, fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("加密PDF无法预览")
        if not 0 <= page_index < doc.page_count:
            raise ValueError("PDF实际页不存在")
        page = doc[page_index]
        width, height = page.rect.width, page.rect.height
        requested = max(.05, fit_width / width) if fit_width else zoom / 100 * 4/3
        maximum = min(math.sqrt(16_000_000/(width*height)), 6000/max(width,height))
        scale = min(requested, maximum)
        # Keep zoom / selection coordinates in logical pixels. Render additional
        # pixels for legible small text and high-DPI monitors, within a memory cap.
        density = max(1, float(pixel_ratio))
        render_scale = min(scale*density, maximum)
        if cancel():
            raise InterruptedError("已取消旧页面渲染")
        words = [{"rect": list(w[:4]), "display_rect": list(fitz.Rect(w[:4]) * page.rotation_matrix), "text": w[4], "block": w[5], "line": w[6]} for w in page.get_text("words", sort=True)]
        evidence_rects = [list(r * page.rotation_matrix) for r in page.search_for(evidence["quote"])] if evidence and evidence["loc_id"].startswith(f"pdf:p{page_index}:") else []
        hits = [{"rect": list(r), "display_rect": list(r * page.rotation_matrix)} for r in page.search_for(query)] if query else []
        pixmap = page.get_pixmap(matrix=fitz.Matrix(render_scale,render_scale), alpha=False)
        if cancel():
            raise InterruptedError("已取消旧页面渲染")
        return {"png": pixmap.tobytes("png"), "words": words, "page_index": page_index,
                "page_count": doc.page_count, "width": width, "height": height,
                "display_width": max(1, round(width*scale)), "display_height": max(1, round(height*scale)),
                "render_density": render_scale/scale,
                "rotation": list(page.rotation_matrix), "effective_zoom": round(scale*75),
                "limited": render_scale < requested*density-.001, "evidence_rects": evidence_rects, "hits": hits, "query": query}

class PdfCanvas(QLabel):
    selectionChanged = Signal(str)
    highlightRequested = Signal()
    zoomRequested = Signal(int)

    def __init__(self):
        super().__init__()
        self.data = None
        self.marks = []
        self.selection = None
        self.manual_selection = None
        self.drag_start = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    def set_page(self, data, marks, file_id):
        previous = self.selection if self.data and self.data.get("file_id") == file_id and self.data["page_index"] == data["page_index"] else None
        self.data = dict(data, file_id=file_id)
        pixmap = QPixmap.fromImage(QImage.fromData(data["png"]))
        pixmap.setDevicePixelRatio(pixmap.width()/data["display_width"])
        self.setPixmap(pixmap)
        self.setFixedSize(data["display_width"],data["display_height"])
        self.marks = marks
        self.selection = previous
        self.manual_selection = None
        if data["query"] and data["hits"]:
            self.selection = None
            self.manual_selection = {"text": data["query"], "anchors": [{"page_index": data["page_index"], "rect": h["rect"]} for h in data["hits"]]}
        self.selectionChanged.emit(self.selected_payload()["text"])
        self.update()

    def display_rect(self, rect):
        return transform_rect(rect,self.data["rotation"])

    def screen_rect(self, rect):
        sx, sy = self.width()/self.data["width"], self.height()/self.data["height"]
        return QRectF(rect[0]*sx,rect[1]*sy,(rect[2]-rect[0])*sx,(rect[3]-rect[1])*sy)

    def selected_payload(self):
        if self.manual_selection:
            return self.manual_selection
        if not self.data or self.selection is None:
            return {"text":"", "anchors":[]}
        start,end = sorted(self.selection)
        words = self.data["words"][start:end+1]
        text, line = [], None
        for word in words:
            key = (word["block"], word["line"])
            if text:
                text.append("\n" if key != line else " ")
            text.append(word["text"]); line = key
        return {"text":"".join(text), "anchors":[{"page_index":self.data["page_index"],"rect":w["rect"]} for w in words]}

    def paintEvent(self,event):
        if not self.data:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap=self.pixmap()
        if pixmap and not pixmap.isNull():
            painter.drawPixmap(QRectF(self.rect()),pixmap,QRectF(0,0,pixmap.width(),pixmap.height()))
        def draw(rect, color, border=False):
            fill = QColor(color); fill.setAlpha(85)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(color),1) if border else Qt.PenStyle.NoPen)
            painter.drawRect(self.screen_rect(rect))
        for mark in self.marks:
            for anchor in mark["anchors"]:
                if anchor.get("page_index") == self.data["page_index"]:
                    draw(self.display_rect(anchor["rect"]), COLORS[mark["color"]][1])
        for rect in self.data["evidence_rects"]:
            draw(rect,"#bc9bff",True)
        for anchor in self.selected_payload()["anchors"]:
            draw(self.display_rect(anchor["rect"]),"#54b5f4",True)
        painter.end()

    def word_at(self,position):
        if not self.data:
            return None
        nearest, distance = None, float("inf")
        for i,word in enumerate(self.data["words"]):
            rect = self.screen_rect(word["display_rect"])
            if rect.contains(position):
                return i
            dx = max(rect.left()-position.x(),0,position.x()-rect.right())
            dy = max(rect.top()-position.y(),0,position.y()-rect.bottom())
            if dx*dx+dy*dy < distance:
                nearest,distance = i,dx*dx+dy*dy
        return nearest if distance <= 24**2 else None

    def select_words(self,start,end):
        if not self.data or not 0 <= start < len(self.data["words"]) or not 0 <= end < len(self.data["words"]):
            return
        self.selection = (start,end); self.manual_selection = None
        self.selectionChanged.emit(self.selected_payload()["text"]); self.update()

    def mousePressEvent(self,event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.drag_start = self.word_at(event.position())
            if self.drag_start is not None:
                self.select_words(self.drag_start,self.drag_start)
            else:
                self.selection = self.manual_selection = None
                self.selectionChanged.emit(""); self.update()
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            end = self.word_at(event.position())
            if end is not None:
                self.select_words(self.drag_start,end)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        self.drag_start = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self,event):
        index = self.word_at(event.position())
        if index is not None:
            words = self.data["words"]; start=end=index
            while start>0 and words[start-1]["block"]==words[index]["block"] and not words[start-1]["text"].endswith((".","!","?","。","！","？")):
                start-=1
            while end+1<len(words) and words[end+1]["block"]==words[index]["block"] and not words[end]["text"].endswith((".","!","?","。","！","？")):
                end+=1
            self.select_words(start,end)

    def contextMenuEvent(self,event):
        menu = QMenu(self)
        copy = menu.addAction("复制选中文字")
        highlight = menu.addAction("保存所选文字高亮")
        copy.setEnabled(bool(self.selected_payload()["text"]))
        highlight.setEnabled(copy.isEnabled())
        result=menu.exec(event.globalPos())
        if result==copy:
            QApplication.clipboard().setText(self.selected_payload()["text"])
        elif result==highlight:
            self.highlightRequested.emit()

    def keyPressEvent(self,event):
        if event.matches(QKeySequence.StandardKey.Copy):
            QApplication.clipboard().setText(self.selected_payload()["text"]); return
        super().keyPressEvent(event)

    def wheelEvent(self,event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomRequested.emit(10 if event.angleDelta().y()>0 else -10)
            event.accept(); return
        super().wheelEvent(event)

class RenderThread(QThread):
    ready = Signal(object)
    failed = Signal(str)
    def __init__(self,kwargs):
        super().__init__()
        self.kwargs=kwargs
        self.cancelled=threading.Event()
    def run(self):
        try:
            self.ready.emit(load_pdf_page(**self.kwargs,cancel=self.cancelled.is_set))
        except InterruptedError:
            pass
        except Exception as exc:
            if not self.cancelled.is_set():
                self.failed.emit(str(exc))

class PdfExportThread(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,root,file_id,target,write_back):
        super().__init__()
        self.root,self.file_id,self.target,self.write_back=root,file_id,target,write_back
        self.cancelled=threading.Event()
    def run(self):
        try:
            from .store import Store
            from .pdf_annotations import save_annotated_pdf
            with Store(self.root) as store:
                self.ready.emit(save_annotated_pdf(store,self.file_id,self.target,self.write_back,self.cancelled.is_set))
        except Exception as exc:
            self.failed.emit(str(exc))

class ReaderPanel(QWidget):
    error=Signal(str)
    changed=Signal(str)
    focusRequested=Signal()
    fileRequested=Signal(str)

    def __init__(self,store,parent=None):
        super().__init__(parent)
        self.store=store
        self.file_id=None; self.kind=None; self.evidence=None
        self.active_worker=None; self.export_worker=None; self.pending=None; self.request_serial=0
        self.zoom_mode="fit"; self.spans=[]; self.base_font=QApplication.font()
        self.layout_ui()

    def layout_ui(self):
        layout=QVBoxLayout(self)
        layout.setContentsMargins(4,4,4,4); layout.setSpacing(4)
        top=QHBoxLayout(); self.label=QLabel("选择附件或证据")
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        top.addWidget(self.label,1)
        self.file_picker=QComboBox(); self.file_picker.setMinimumWidth(80)
        self.file_picker.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.file_picker.currentIndexChanged.connect(lambda index:self.fileRequested.emit(self.file_picker.itemData(index)) if index>=0 else None)
        top.addWidget(self.file_picker,1); self.file_picker.hide()
        self.focus_button=QPushButton("阅读模式"); self.focus_button.clicked.connect(self.focusRequested.emit); top.addWidget(self.focus_button)
        self.tools_button=QPushButton("更多阅读操作 ▾"); self.tools_button.setCheckable(True); top.addWidget(self.tools_button)
        layout.addLayout(top)
        zoomrow=QHBoxLayout()
        self.previous=QPushButton("上一页"); self.previous.clicked.connect(lambda:self.page.setValue(self.page.value()-1)); zoomrow.addWidget(self.previous)
        self.page=QSpinBox(); self.page.setRange(1,1); self.page.setPrefix("页 "); self.page.setToolTip("PDF实际页面，非印刷页码"); self.page.valueChanged.connect(self.render_pdf); zoomrow.addWidget(self.page)
        self.following=QPushButton("下一页"); self.following.clicked.connect(lambda:self.page.setValue(self.page.value()+1)); zoomrow.addWidget(self.following)
        for text,amount in [("－",-25),("＋",25)]:
            button=QPushButton(text); button.setFixedWidth(26); button.clicked.connect(lambda _,n=amount:self.adjust_zoom(n)); zoomrow.addWidget(button)
        self.zoom=QSpinBox(); self.zoom.setRange(25,400); self.zoom.setValue(100); self.zoom.setSuffix("%"); self.zoom.valueChanged.connect(self.manual_zoom); zoomrow.addWidget(self.zoom)
        fit=QPushButton("适合宽度"); fit.clicked.connect(self.fit_width); zoomrow.addWidget(fit)
        actual=QPushButton("100%"); actual.clicked.connect(lambda:self.zoom.setValue(100) if self.zoom.value()!=100 else self.manual_zoom(100)); zoomrow.addWidget(actual)
        self.hint=QLabel("PDF拖选文字或双击句子；Ctrl+滚轮缩放")
        self.hint.hide()
        zoomrow.addStretch(); layout.addLayout(zoomrow)
        self.tools=QWidget(); toolslayout=QVBoxLayout(self.tools); toolslayout.setContentsMargins(0,0,0,0); toolslayout.setSpacing(4)
        findrow=QHBoxLayout(); self.query=QLineEdit(); self.query.setPlaceholderText("粘贴当前页句子查找；重复文字会选中全部匹配区域")
        self.query.returnPressed.connect(self.find_sentence); findrow.addWidget(self.query)
        find=QPushButton("查找句子"); find.clicked.connect(self.find_sentence); findrow.addWidget(find); toolslayout.addLayout(findrow)
        exportrow=QHBoxLayout()
        self.export_copy=QPushButton("另存带批注 PDF…"); self.export_copy.clicked.connect(lambda:self.choose_pdf_export(False)); exportrow.addWidget(self.export_copy)
        self.write_original=QPushButton("写回原 PDF…"); self.write_original.clicked.connect(lambda:self.choose_pdf_export(True)); exportrow.addWidget(self.write_original)
        self.cancel_export=QPushButton("取消保存"); self.cancel_export.setEnabled(False); self.cancel_export.clicked.connect(self.cancel_pdf_export); exportrow.addWidget(self.cancel_export)
        exportrow.addStretch(); toolslayout.addLayout(exportrow)
        layout.addWidget(self.tools); self.tools.hide(); self.tools_button.toggled.connect(self.tools.setVisible)
        pages=QWidget(); pagelayout=QVBoxLayout(pages); pagelayout.setContentsMargins(0,0,0,0)
        self.canvas=PdfCanvas(); self.canvas.selectionChanged.connect(self.selection_changed); self.canvas.highlightRequested.connect(self.save_selection); self.canvas.zoomRequested.connect(self.adjust_zoom)
        self.scroll=QScrollArea(); self.scroll.setWidget(self.canvas); self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll.setToolTip("拖选文字或双击句子；Ctrl+滚轮缩放；更多阅读操作中可查找句子或保存PDF批注")
        self.scroll.setStyleSheet("QScrollArea{background:#e1e7ed;border:1px solid #ccd5dc}")
        self.scroll.viewport().installEventFilter(self)
        pagelayout.addWidget(self.scroll)
        self.doc_text=QPlainTextEdit(); self.doc_text.setReadOnly(True); self.doc_text.selectionChanged.connect(lambda:self.selection_changed(self.doc_text.textCursor().selectedText()))
        pagelayout.addWidget(self.doc_text); self.doc_text.hide()
        layout.addWidget(pages,1)
        markpanel=QWidget(); self.mark_panel=markpanel; marklayout=QVBoxLayout(markpanel); marklayout.setContentsMargins(0,0,0,0)
        marklayout.setSpacing(4)
        row=QHBoxLayout(); self.color=QComboBox()
        for key,(name,hexcolor) in COLORS.items(): self.color.addItem(name,key)
        row.addWidget(self.color)
        self.note=QLineEdit(); self.note.setMinimumWidth(60); self.note.setPlaceholderText("可选批注"); row.addWidget(self.note,1)
        for label,callback in [("保存高亮",self.save_selection),("复制选中",self.copy_selection)]:
            button=QPushButton(label); button.clicked.connect(callback); row.addWidget(button)
        self.marks_button=QPushButton("批注列表 ▾"); self.marks_button.setCheckable(True); row.addWidget(self.marks_button)
        marklayout.addLayout(row)
        self.mark_details=QWidget(); details=QVBoxLayout(self.mark_details); details.setContentsMargins(0,0,0,0); details.setSpacing(4)
        actions=QHBoxLayout()
        for label,callback in [("修改批注",self.edit_mark),("删除高亮",self.delete_mark)]:
            button=QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(); details.addLayout(actions)
        self.marks=QTreeWidget(); self.marks.setHeaderLabels(["位置 / 颜色","高亮原文","批注"]); self.marks.setColumnWidth(0,130); self.marks.setColumnWidth(1,270)
        self.marks.setMinimumHeight(65); self.marks.setMaximumHeight(120)
        self.marks.itemDoubleClicked.connect(self.jump_mark); details.addWidget(self.marks)
        marklayout.addWidget(self.mark_details); self.mark_details.hide(); self.marks_button.toggled.connect(self.mark_details.setVisible)
        layout.addWidget(markpanel)
        self.status=StatusLabel("高亮先存入数据库；需要在其他阅读器查看时，另存或写回PDF。")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.status.setFixedHeight(20); self.status.setToolTip(self.status.text()); layout.addWidget(self.status)
        self.fit_timer=QTimer(self); self.fit_timer.setSingleShot(True); self.fit_timer.timeout.connect(self.render_pdf)
        self.shortcuts=[]
        for sequence,callback in [("Ctrl++",lambda:self.adjust_zoom(25)),("Ctrl+=",lambda:self.adjust_zoom(25)),("Ctrl+-",lambda:self.adjust_zoom(-25)),("Ctrl+0",self.fit_width)]:
            shortcut=QShortcut(QKeySequence(sequence),self); shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); shortcut.activated.connect(callback); self.shortcuts.append(shortcut)

    def eventFilter(self,watched,event):
        if watched is self.scroll.viewport() and event.type()==QEvent.Type.Resize and self.zoom_mode=="fit" and self.kind=="pdf":
            if hasattr(self,"fit_timer"): self.fit_timer.start(150)
        return super().eventFilter(watched,event)

    def set_files(self,files):
        self.file_picker.blockSignals(True); self.file_picker.clear()
        roles={"main":"正文","si":"SI","version":"正文版本","preprint":"预印本","other":"附件"}
        for file in files:
            self.file_picker.addItem(f"{roles.get(file['role'],'附件')} · {file['name']}",file["file_id"])
            self.file_picker.setItemData(self.file_picker.count()-1,file["name"],Qt.ItemDataRole.ToolTipRole)
        self.file_picker.blockSignals(False)
        self.file_picker.setVisible(bool(files)); self.label.setVisible(not files)

    def clear_file(self,message="当前文献没有可预览附件"):
        self.stop(); self.file_id=None; self.kind=None; self.evidence=None
        self.canvas.clear(); self.canvas.data=None; self.canvas.marks=[]
        self.canvas.setFixedSize(1,1); self.scroll.show(); self.doc_text.hide()
        self.canvas.selection=self.canvas.manual_selection=None
        self.doc_text.clear(); self.marks.clear(); self.query.clear(); self.note.clear()
        self.export_copy.setEnabled(False); self.write_original.setEnabled(False)
        self.label.setText(message); self.status.setText(message)

    def open_file(self,file_id,evidence=None):
        self.file_id=file_id; self.evidence=evidence; self.query.clear(); self.note.clear()
        self.file_picker.blockSignals(True); self.file_picker.setCurrentIndex(self.file_picker.findData(file_id)); self.file_picker.blockSignals(False)
        self.request_serial+=1; self.pending=None
        if self.active_worker: self.active_worker.cancelled.set()
        file=self.store.file(file_id); extraction=json.loads(file["extracted"])
        self.kind=Path(file["path"]).suffix.lstrip(".").lower()
        self.label.setText(file["name"]); self.label.setToolTip(file["reason"])
        state=self.store.reader_state(file_id); self.zoom_mode=state["zoom_mode"]
        self.zoom.blockSignals(True); self.zoom.setValue(state["zoom"]); self.zoom.blockSignals(False)
        pdf=self.kind=="pdf"
        self.export_copy.setEnabled(pdf and not self.export_worker); self.write_original.setEnabled(pdf and not self.export_worker)
        for control in [self.page,self.previous,self.following]: control.setVisible(pdf)
        self.scroll.setVisible(pdf); self.doc_text.setVisible(not pdf)
        self.load_marks()
        if pdf:
            page=state["page_index"]+1
            if evidence:
                loc=next((x for x in extraction["locations"] if x["loc_id"]==evidence["loc_id"]),None)
                if loc: page=loc["page_index"]+1
            self.page.blockSignals(True); self.page.setRange(1,max(page,1)); self.page.setValue(page); self.page.blockSignals(False)
            self.canvas.clear(); self.canvas.data=None
            self.render_pdf()
        else:
            self.doc_text.setPlainText("")
            text,self.spans=docx_display(extraction["locations"])
            self.doc_text.setPlainText(text or file["reason"])
            self.apply_docx_zoom(); self.paint_docx_marks()
            if evidence:
                cursor=self.doc_text.document().find("["+evidence["loc_id"]+"]")
                if not cursor.isNull(): self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()
            self.hint.setText("DOCX直接选中文字后保存高亮；位置使用段落/表格行")
            self.status.setText(file["reason"])

    def manual_zoom(self,value):
        self.zoom_mode="manual"
        if self.kind=="pdf": self.render_pdf()
        else: self.apply_docx_zoom(); self.remember()

    def adjust_zoom(self,amount):
        value=max(25,min(400,self.zoom.value()+amount))
        self.zoom.setValue(value)

    def fit_width(self):
        self.zoom_mode="fit"
        if self.kind=="pdf": self.render_pdf()
        else: self.zoom.setValue(100); self.apply_docx_zoom()

    def apply_docx_zoom(self):
        font=QFont(self.base_font); font.setPointSizeF(max(4,(self.base_font.pointSizeF() if self.base_font.pointSizeF()>0 else 10)*self.zoom.value()/100)); self.doc_text.setFont(font)

    def remember(self):
        if self.file_id: self.store.save_reader_state(self.file_id,self.page.value()-1 if self.kind=="pdf" else 0,self.zoom.value(),self.zoom_mode)

    def render_pdf(self,*_,query=""):
        if self.kind!="pdf" or not self.file_id: return
        self.request_serial+=1
        self.pending={"serial":self.request_serial,"file_id":self.file_id,"page_index":self.page.value()-1,"zoom":self.zoom.value(),"fit_width":max(80,self.scroll.viewport().width()-24) if self.zoom_mode=="fit" else None,"evidence":self.evidence,"query":query}
        self.status.setText("正在后台渲染页面…")
        if self.active_worker: self.active_worker.cancelled.set()
        else: self.start_render()

    def start_render(self):
        if not self.pending or self.kind!="pdf": return
        request=self.pending; self.pending=None
        file=self.store.file(request["file_id"])
        kwargs={k:request[k] for k in ["page_index","zoom","fit_width","evidence","query"]}
        kwargs.update(path=self.store.path(request["file_id"]),expected_hash=file["sha256"],pixel_ratio=max(2,self.devicePixelRatioF()))
        worker=RenderThread(kwargs); self.active_worker=worker
        def display(data):
            if request["serial"]!=self.request_serial or request["file_id"]!=self.file_id: return
            self.page.blockSignals(True); self.page.setRange(1,data["page_count"]); self.page.setValue(data["page_index"]+1); self.page.blockSignals(False)
            self.zoom.blockSignals(True); self.zoom.setValue(max(25,min(400,data["effective_zoom"]))); self.zoom.blockSignals(False)
            marks=[m for m in self.store.highlights(self.file_id) if m["file_sha256"]==file["sha256"]]
            self.canvas.set_page(data,marks,self.file_id)
            self.hint.setText("PDF拖选文字或双击句子；Ctrl+滚轮缩放")
            text=f"实际页 {data['page_index']+1}/{data['page_count']} · {data['effective_zoom']}% · {len(marks)}条高亮"
            if not data["words"]: text+=" · 本页无可选文字，扫描页需OCR后才能按句子高亮"
            if data["limited"]: text+=" · 页面过大，已限制预览像素以保护内存"
            if data["query"]: text+=f" · 找到 {len(data['hits'])} 个文本区域" if data["hits"] else " · 本页未匹配，请拖选文字或缩短句子"
            self.status.setText(text); self.remember()
        def failed(message):
            if request["serial"]==self.request_serial:
                self.status.setText(message); self.error.emit(message)
        def finished():
            self.active_worker=None; worker.deleteLater()
            if self.pending: self.start_render()
        worker.ready.connect(display); worker.failed.connect(failed); worker.finished.connect(finished); worker.start()

    def find_sentence(self):
        query=self.query.text().strip()
        if not query: return
        if self.kind=="pdf": self.render_pdf(query=query)
        elif self.kind=="docx":
            cursor=self.doc_text.document().find(query)
            if cursor.isNull(): self.status.setText("未匹配到句子，请直接选择原文")
            else: self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()

    def selection_changed(self,text):
        if text: self.status.setText(f"已选 {len(text)} 字符：{text[:140].replace(chr(10),' ')}")

    def selection_payload(self):
        if self.kind=="pdf": return self.canvas.selected_payload()
        cursor=self.doc_text.textCursor()
        return docx_selection(self.spans,cursor.selectionStart(),cursor.selectionEnd())

    def copy_selection(self):
        QApplication.clipboard().setText(self.selection_payload()["text"])

    def save_selection(self):
        if self.export_worker:
            self.status.setText("正在保存PDF，请等待结束后修改高亮"); return
        if self.kind not in {"pdf","docx"} or not self.file_id:
            self.error.emit("请先打开可读取的PDF或DOCX"); return
        if self.kind=="pdf" and self.active_worker:
            self.status.setText("请等待当前页面渲染完成后保存高亮"); return
        try:
            payload=self.selection_payload()
            mark_id=self.store.save_highlight(self.file_id,self.kind,payload["text"],payload["anchors"],self.color.currentData(),self.note.text())
            self.note.clear(); self.load_marks(mark_id)
            if self.kind=="pdf": self.canvas.selection=self.canvas.manual_selection=None; self.canvas.update()
            else: self.paint_docx_marks()
            self.status.setText("高亮与批注已保存；重新打开或移动数据目录后仍保留，原件未修改")
            self.changed.emit(self.file_id)
        except Exception as exc: self.error.emit(str(exc))

    def load_marks(self,select_id=None):
        self.marks.clear()
        marks=self.store.highlights(self.file_id) if self.file_id else []
        if self.file_id: marks=[m for m in marks if m["file_sha256"]==self.store.file(self.file_id)["sha256"]]
        self.canvas.marks=marks; self.canvas.update()
        for mark in marks:
            anchor=mark["anchors"][0]
            position=f"实际页 {anchor['page_index']+1}" if mark["kind"]=="pdf" else anchor["loc_id"]
            item=QTreeWidgetItem([position+" / "+COLORS[mark["color"]][0],mark["text"],mark["note"]])
            item.setData(0,Qt.ItemDataRole.UserRole,mark)
            for i in range(3): item.setToolTip(i,item.text(i))
            self.marks.addTopLevelItem(item)
            if mark["mark_id"]==select_id: self.marks.setCurrentItem(item)

    def paint_docx_marks(self):
        selections=[]
        spans={s["loc_id"]:s for s in self.spans}
        for mark in self.store.highlights(self.file_id):
            if mark["kind"]!="docx" or mark["file_sha256"]!=self.store.file(self.file_id)["sha256"]: continue
            for anchor in mark["anchors"]:
                span=spans.get(anchor["loc_id"])
                if not span: continue
                selection=QTextEdit.ExtraSelection(); selection.cursor=QTextCursor(self.doc_text.document())
                selection.cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["start"]]))
                selection.cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["end"]]),QTextCursor.MoveMode.KeepAnchor)
                selection.format.setBackground(QColor(COLORS[mark["color"]][1])); selections.append(selection)
        self.doc_text.setExtraSelections(selections)

    def jump_mark(self,item,*_):
        mark=item.data(0,Qt.ItemDataRole.UserRole); anchor=mark["anchors"][0]
        if mark["kind"]=="pdf":
            if self.page.value()!=anchor["page_index"]+1: self.page.setValue(anchor["page_index"]+1)
            elif self.canvas.data: self.scroll.ensureVisible(int(self.canvas.screen_rect(self.canvas.display_rect(anchor["rect"])).center().x()),int(self.canvas.screen_rect(self.canvas.display_rect(anchor["rect"])).center().y()),40,80)
        else:
            span=next((s for s in self.spans if s["loc_id"]==anchor["loc_id"]),None)
            if span:
                cursor=self.doc_text.textCursor(); cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["start"]])); cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["end"]]),QTextCursor.MoveMode.KeepAnchor)
                self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()

    def delete_mark(self):
        item=self.marks.currentItem()
        if not item: self.status.setText("请先在高亮列表选择一条记录"); return
        mark=item.data(0,Qt.ItemDataRole.UserRole)
        try:
            self.store.delete_highlight(mark["mark_id"],self.file_id); self.load_marks()
            if self.kind=="docx": self.paint_docx_marks()
            self.status.setText("高亮已删除，修订历史保留"); self.changed.emit(self.file_id)
        except Exception as exc: self.error.emit(str(exc))

    def edit_mark(self):
        item=self.marks.currentItem()
        if not item: self.status.setText("请先在高亮列表选择一条记录"); return
        mark=item.data(0,Qt.ItemDataRole.UserRole)
        dialog=QDialog(self); dialog.setWindowTitle("修改高亮颜色与批注"); form=QFormLayout(dialog)
        colors=QComboBox()
        for key,(label,_) in COLORS.items(): colors.addItem(label,key)
        colors.setCurrentIndex(colors.findData(mark["color"])); note=QPlainTextEdit(mark["note"])
        form.addRow("颜色",colors); form.addRow("批注",note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec():
            try:
                self.store.save_highlight(self.file_id,mark["kind"],mark["text"],mark["anchors"],colors.currentData(),note.toPlainText(),mark["mark_id"]); self.load_marks(mark["mark_id"])
                if self.kind=="docx": self.paint_docx_marks()
                self.changed.emit(self.file_id)
            except Exception as exc: self.error.emit(str(exc))

    def choose_pdf_export(self,write_back):
        if self.kind!="pdf" or not self.file_id:
            self.error.emit("仅PDF支持文件批注写入；DOCX仍保存在工具数据库"); return
        if self.export_worker: return
        if write_back:
            dialog=QFileDialog(self,"选择最初导入的原PDF（写回前自动备份）")
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            dialog.setNameFilter("PDF (*.pdf)")
            dialog.setLabelText(QFileDialog.DialogLabel.Accept,"写回并备份")
            if not dialog.exec(): return
            target=dialog.selectedFiles()[0]
        else:
            folder=self.store.root/"annotated"; folder.mkdir(exist_ok=True)
            name=Path(self.store.file(self.file_id)["name"]).stem[:120]+"_批注.pdf"
            target,_=QFileDialog.getSaveFileName(self,"另存标准PDF高亮与批注",str(folder/name),"PDF (*.pdf)")
            if not target: return
        self.start_pdf_export(target,write_back)

    def start_pdf_export(self,target,write_back=False):
        if self.export_worker: return
        worker=PdfExportThread(self.store.root,self.file_id,target,write_back)
        self.export_worker=worker
        self.export_copy.setEnabled(False); self.write_original.setEnabled(False)
        self.cancel_export.setEnabled(True); self.mark_panel.setEnabled(False)
        self.status.setText("正在后台写入标准PDF批注；已有目标文件会先自动备份…")
        worker.ready.connect(self.pdf_saved)
        worker.failed.connect(lambda message:self.status.setText(message) if worker.cancelled.is_set() else self.error.emit(message))
        def finished():
            self.export_worker=None
            self.export_copy.setEnabled(self.kind=="pdf"); self.write_original.setEnabled(self.kind=="pdf")
            self.cancel_export.setEnabled(False); self.mark_panel.setEnabled(True)
            worker.deleteLater()
        worker.finished.connect(finished); worker.start()

    def cancel_pdf_export(self):
        if self.export_worker:
            self.export_worker.cancelled.set(); self.status.setText("正在取消PDF保存；已替换目标前的步骤可取消")

    def pdf_saved(self,report):
        self.status.setText(f"PDF已保存：{report['target']}；{report['annotation_count']}条标准高亮批注")
        text="已把高亮和批注写入PDF，可在支持批注的阅读器中查看。\n\n"+report["target"]
        if report["backup"]: text+="\n\n写入前的备份：\n"+report["backup"]
        if report["warnings"]: text+="\n\n"+"\n".join(report["warnings"])
        QMessageBox.information(self,"PDF批注已保存",text)

    def stop(self):
        self.pending=None; self.request_serial+=1; self.fit_timer.stop()
        if self.active_worker: self.active_worker.cancelled.set()
        if self.export_worker: self.export_worker.cancelled.set()
