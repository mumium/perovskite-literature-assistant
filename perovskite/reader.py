"""PDF text selection and portable annotations, kept outside the original files."""
import json
import math
import threading
from contextlib import nullcontext
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QRectF,QSize
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QImage, QFont, QTextCursor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSpinBox, QPushButton,
    QScrollArea, QPlainTextEdit, QComboBox, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QApplication, QMenu, QTextEdit, QDialog, QDialogButtonBox, QFormLayout, QSplitter, QFileDialog, QMessageBox, QSizePolicy, QAbstractSpinBox, QAbstractItemView, QProgressBar,QSlider,QColorDialog,QInputDialog)
from .editing import InlineDelegate,EDIT_ROLE
from .ui_icons import icon
from .note_style import default_note_style

from .extract import digest, PDF_LOCK

COLORS = {"yellow": ("黄色", "#ffd84d"), "green": ("绿色", "#7ddc91"), "blue": ("蓝色", "#77caff"), "pink": ("粉色", "#ff9bc4")}

class StatusLabel(QLabel):
    def setText(self,text):
        super().setText(text)
        self.setToolTip(text)

class PageSpinBox(QSpinBox):
    def stepBy(self,steps): super().stepBy(-steps)
    def stepEnabled(self):
        flags=QAbstractSpinBox.StepEnabledFlag.StepNone
        if self.value()>self.minimum(): flags|=QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
        if self.value()<self.maximum(): flags|=QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
        return flags

def slider_zoom(value):
    return round(100+value*(.75 if value<0 else 3))

def zoom_slider(value):
    return round((value-100)/(.75 if value<100 else 3))

class ZoomSlider(QSlider):
    resetRequested=Signal()
    def __init__(self,*args):
        super().__init__(*args); self.sliderMoved.connect(self.snap_center)
    def snap_center(self,value):
        # Small detent only while dragging: typed 99/101% remains available.
        if self.isSliderDown() and value!=0 and abs(value)<=3: self.setSliderPosition(0)
    def mousePressEvent(self,event):
        super().mousePressEvent(event); self.snap_center(self.value())
    def mouseDoubleClickEvent(self,event): self.setValue(0); self.resetRequested.emit(); event.accept()

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

def load_pdf_page(path, page_index, zoom=100, fit_width=None, evidence=None, query="", expected_hash=None, pixel_ratio=2, text_data=None, cancel=lambda:False, document=None, raw_image=False):
    import fitz
    if expected_hash and digest(path, cancel) != expected_hash:
        raise ValueError("附件哈希已变化，请从原件重新导入；未把高亮关联到改变后的文件")
    with PDF_LOCK, (nullcontext(document) if document is not None else fitz.open(path)) as doc:
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
        density = max(.01, float(pixel_ratio))
        render_scale = min(scale*density, maximum)
        if cancel():
            raise InterruptedError("已取消旧页面渲染")
        if text_data:
            words=text_data['words']; characters=text_data['characters']
        else:
            words = [{"rect": list(w[:4]), "display_rect": list(fitz.Rect(w[:4]) * page.rotation_matrix), "text": w[4], "block": w[5], "line": w[6]} for w in page.get_text("words", sort=True)]
            characters=[]
            for block_index,block in enumerate(page.get_text('rawdict',sort=True)['blocks']):
                for line_index,line in enumerate(block.get('lines',[])):
                    for span in line.get('spans',[]):
                        for char in span.get('chars',[]):
                            rect=fitz.Rect(char['bbox'])
                            if rect.width>0 and rect.height>0:
                                characters.append({'rect':list(rect),'display_rect':list(rect*page.rotation_matrix),'text':char['c'],'block':block_index,'line':line_index})
        evidence_rects = [list(r * page.rotation_matrix) for r in page.search_for(evidence["quote"])] if evidence and evidence["loc_id"].startswith(f"pdf:p{page_index}:") else []
        hits = [{"rect": list(r), "display_rect": list(r * page.rotation_matrix)} for r in page.search_for(query)] if query else []
        display_list=page.get_displaylist()
        pixmap = display_list.get_pixmap(matrix=fitz.Matrix(render_scale,render_scale),colorspace=fitz.csRGB, alpha=False)
        if cancel():
            raise InterruptedError("已取消旧页面渲染")
        image=QImage(pixmap.samples,pixmap.width,pixmap.height,pixmap.stride,QImage.Format.Format_RGB888).copy() if raw_image else None
        return {"png": b'' if raw_image else pixmap.tobytes("png"), "image":image, "display_list":display_list if raw_image else None,"words": words, "characters":characters,"page_index": page_index,
                "page_count": doc.page_count, "width": width, "height": height,
                "display_width": max(1, round(width*scale)), "display_height": max(1, round(height*scale)),
                "render_density": render_scale/scale,
                "rotation": list(page.rotation_matrix), "effective_zoom": round(scale*75),
                "limited": render_scale < requested*density-.001, "evidence_rects": evidence_rects, "hits": hits, "query": query}

class PdfCanvas(QLabel):
    selectionChanged = Signal(str)
    highlightRequested = Signal()
    zoomRequested = Signal(int)
    markSelected = Signal(str)
    deleteRequested = Signal()
    dragRequested = Signal(object,object)
    horizontalRequested=Signal(int)
    noteStyleChanged=Signal(str,object)
    noteEditRequested=Signal(str)

    def __init__(self):
        super().__init__()
        self.data = None
        self._image=QImage()
        self.hd=None; self.note_boxes={}
        self.marks = []
        self.selection = None
        self.manual_selection = None
        self.drag_start = None
        self.selected_mark=None
        self.setStyleSheet('background:white;color:#7a8793')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    def set_page(self, data, marks, file_id):
        previous = self.selection if self.data and self.data.get("file_id") == file_id and self.data["page_index"] == data["page_index"] else None
        self.data = dict(data, file_id=file_id)
        self._image=data.get('image') or QImage.fromData(data['png'])
        self.setFixedSize(data["display_width"],data["display_height"])
        self.marks = marks
        self.selection = previous
        self.manual_selection = None
        if data["query"] and data["hits"]:
            self.selection = None
            self.manual_selection = {"text": data["query"], "anchors": [{"page_index": data["page_index"], "rect": h["rect"]} for h in data["hits"]]}
        self.selectionChanged.emit(self.selected_payload()["text"])
        self.update()
        self.sync_notes()

    def set_display_scale(self,scale):
        geometry=getattr(self,'geometry_info',None) or self.data
        if geometry:
            self.setFixedSize(max(1,round(geometry['width']*scale)),max(1,round(geometry['height']*scale)))
            for box in self.note_boxes.values(): box.sync_geometry()

    def sync_notes(self):
        from .note_boxes import PageNote
        desired={}
        if self.data:
            for mark in self.marks:
                if not mark['note'] or mark['kind']!='pdf': continue
                style=mark.get('appearance') or default_note_style(mark,self.data['width'],self.data['height'],self.display_rect(mark['anchors'][0]['rect']))
                if style['page_index']!=self.data['page_index']: continue
                desired[mark['mark_id']]=(mark,style)
        for mid in list(self.note_boxes):
            if mid not in desired:
                self.note_boxes.pop(mid).deleteLater()
        for mid,(mark,style) in desired.items():
            box=self.note_boxes.get(mid)
            if box:
                box.text=mark['note']; box.style=dict(style); box.sync_geometry()
            else:
                box=PageNote(self,mark,style); self.note_boxes[mid]=box
                box.selected.connect(self.markSelected.emit); box.changed.connect(self.noteStyleChanged.emit); box.editRequested.connect(self.noteEditRequested.emit)
            box.setEnabled(getattr(self,'notes_editable',True))

    def release_bitmap(self,drop_data=False):
        self._image=QImage()
        self.hd=None
        if self.data: self.data['png']=b''
        if drop_data: self.data=None
        if drop_data: self.sync_notes()
        self.setText(f"实际页 {getattr(self,'page_index',0)+1} · 等待全文预加载")

    def pixmap(self):
        pixmap=QPixmap.fromImage(self._image)
        if self.data and not pixmap.isNull(): pixmap.setDevicePixelRatio(pixmap.width()/self.data['display_width'])
        return pixmap

    def clear_selection(self):
        self.selection=self.manual_selection=None; self.update()

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
        words = self.data.get("characters",[])[start:end+1]
        text, line = [], None
        for word in words:
            key = (word["block"], word["line"])
            if text:
                if key!=line: text.append("\n")
            text.append(word["text"]); line = key
        anchors=[]
        previous=None
        for char in words:
            key=(char['block'],char['line'])
            rect=char['rect']
            if anchors and previous==key:
                old=anchors[-1]['rect']; anchors[-1]['rect']=[min(old[0],rect[0]),min(old[1],rect[1]),max(old[2],rect[2]),max(old[3],rect[3])]
            else: anchors.append({'page_index':self.data['page_index'],'rect':list(rect)})
            previous=key
        return {"text":"".join(text), "anchors":anchors}

    def paintEvent(self,event):
        if not self.data:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._image.isNull():
            painter.drawImage(QRectF(self.rect()),self._image,QRectF(self._image.rect()))
        if self.hd:
            painter.drawImage(self.screen_rect(self.hd['rect']),self.hd['image'],QRectF(self.hd['image'].rect()))
        def draw(rect, color, border=False):
            fill = QColor(color); fill.setAlpha(85)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(color),1) if border else Qt.PenStyle.NoPen)
            painter.drawRect(self.screen_rect(rect))
        for mark in self.marks:
            for anchor in mark["anchors"]:
                if anchor.get("page_index") == self.data["page_index"]:
                    draw(self.display_rect(anchor["rect"]), COLORS[mark["color"]][1],mark['mark_id']==self.selected_mark)
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
        words=self.data['words'][min(start,end):max(start,end)+1]
        text=[]; previous=None
        for word in words:
            key=(word['block'],word['line'])
            if text: text.append('\n' if key!=previous else ' ')
            text.append(word['text']); previous=key
        self.selection=None
        self.manual_selection={'text':''.join(text),'anchors':[{'page_index':self.data['page_index'],'rect':w['rect']} for w in words]}
        self.selectionChanged.emit(self.selected_payload()["text"]); self.update()

    def char_at(self,position):
        if not self.data: return None
        nearest,distance=None,float('inf')
        for index,char in enumerate(self.data.get('characters',[])):
            rect=self.screen_rect(char['display_rect'])
            if rect.contains(position): return index
            dx=max(rect.left()-position.x(),0,position.x()-rect.right()); dy=max(rect.top()-position.y(),0,position.y()-rect.bottom())
            if dx*dx+dy*dy<distance: nearest,distance=index,dx*dx+dy*dy
        return nearest if distance<=24**2 else None

    def select_chars(self,start,end):
        self.selection=(start,end); self.manual_selection=None
        self.selectionChanged.emit(self.selected_payload()['text']); self.update()

    def mousePressEvent(self,event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            found_mark=False
            for mark in reversed(self.marks):
                if self.data and any(a.get('page_index')==self.data['page_index'] and self.screen_rect(self.display_rect(a['rect'])).contains(event.position()) for a in mark['anchors']):
                    self.markSelected.emit(mark['mark_id']); found_mark=True; break
            if not found_mark: self.markSelected.emit('')
            self.drag_start = self.char_at(event.position())
            if self.drag_start is not None:
                self.select_chars(self.drag_start,self.drag_start)
            else:
                self.selection = self.manual_selection = None
                self.selectionChanged.emit(""); self.update()
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self.rect().contains(event.position().toPoint()):
                self.dragRequested.emit(self.drag_start,event.globalPosition().toPoint()); event.accept(); return
            end = self.char_at(event.position())
            if end is not None:
                self.select_chars(self.drag_start,end)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        self.drag_start = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self,event):
        index = self.char_at(event.position())
        if index is not None:
            words = self.data["characters"]; start=end=index
            while start>0 and words[start-1]["block"]==words[index]["block"] and not words[start-1]["text"].endswith((".","!","?","。","！","？")):
                start-=1
            while end+1<len(words) and words[end+1]["block"]==words[index]["block"] and not words[end]["text"].endswith((".","!","?","。","！","？")):
                end+=1
            self.select_chars(start,end)

    def contextMenuEvent(self,event):
        menu = QMenu(self)
        copy = menu.addAction("复制选中文字")
        highlight = menu.addAction('取消高亮（Shift+H）' if self.selected_mark else '高亮（Shift+H）')
        copy.setEnabled(bool(self.selected_payload()["text"]))
        highlight.setEnabled(copy.isEnabled() or bool(self.selected_mark))
        result=menu.exec(event.globalPos())
        if result==copy:
            QApplication.clipboard().setText(getattr(self,'document_payload',self.selected_payload)()["text"])
        elif result==highlight:
            self.highlightRequested.emit()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Delete:
            self.deleteRequested.emit(); event.accept(); return
        if event.matches(QKeySequence.StandardKey.Copy):
            QApplication.clipboard().setText(getattr(self,'document_payload',self.selected_payload)()["text"]); return
        super().keyPressEvent(event)

    def wheelEvent(self,event):
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta=event.pixelDelta().x() or event.pixelDelta().y() or event.angleDelta().x() or event.angleDelta().y()
            self.horizontalRequested.emit(delta); event.accept(); return
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
        self.export_worker=None; self.request_serial=0
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
        self.tools_button=QPushButton("保存批注 ▾"); self.tools_button.setCheckable(True); top.addWidget(self.tools_button)
        layout.addLayout(top)
        zoomrow=QHBoxLayout()
        self.page=PageSpinBox(); self.page.setRange(1,1); self.page.setPrefix("页 "); self.page.setToolTip("实际页码：下箭头向后翻页，上箭头向前；也可直接滚动全文"); self.page.valueChanged.connect(self.go_to_page); zoomrow.addWidget(self.page)
        self.zoom_minus=QPushButton('－'); self.zoom_minus.setFixedWidth(28); self.zoom_minus.clicked.connect(lambda:self.adjust_zoom(-25)); zoomrow.addWidget(self.zoom_minus)
        self.zoom_slider=ZoomSlider(Qt.Orientation.Horizontal); self.zoom_slider.setRange(-100,100); self.zoom_slider.setValue(0)
        self.zoom_slider.setMinimumWidth(90); self.zoom_slider.setMaximumWidth(230); self.zoom_slider.setTickPosition(QSlider.TickPosition.TicksBelow); self.zoom_slider.setTickInterval(100)
        self.zoom_slider.setToolTip('缩放：左端25%，中间100%轻微吸附，右端400%；双击恢复100%')
        self.zoom_slider.resetRequested.connect(lambda:self.zoom.setValue(100))
        self.zoom_slider.valueChanged.connect(lambda n:self.zoom.setValue(slider_zoom(n))); zoomrow.addWidget(self.zoom_slider,1)
        self.zoom_plus=QPushButton('＋'); self.zoom_plus.setFixedWidth(28); self.zoom_plus.clicked.connect(lambda:self.adjust_zoom(25)); zoomrow.addWidget(self.zoom_plus)
        self.zoom=QSpinBox(); self.zoom.setRange(25,400); self.zoom.setValue(100); self.zoom.setSuffix('%'); self.zoom.setSingleStep(1); self.zoom.setKeyboardTracking(False)
        self.zoom.setMinimumSize(88,26); self.zoom.setStyleSheet('QSpinBox{padding:2px 20px 2px 3px;font-size:12px} QSpinBox::up-button{width:18px;subcontrol-position:top right} QSpinBox::down-button{width:18px;subcontrol-position:bottom right}')
        self.zoom.setToolTip('输入百分比，或点击右侧上下箭头精调1%'); self.zoom.valueChanged.connect(self.manual_zoom); zoomrow.addWidget(self.zoom)
        fit=QPushButton("适合宽度"); fit.clicked.connect(self.fit_width); zoomrow.addWidget(fit)
        self.hint=QLabel("PDF拖选文字或双击句子；Ctrl+滚轮缩放")
        self.hint.hide()
        zoomrow.addStretch(); layout.addLayout(zoomrow)
        self.tools=QWidget(); toolslayout=QVBoxLayout(self.tools); toolslayout.setContentsMargins(0,0,0,0); toolslayout.setSpacing(4)
        findrow=QHBoxLayout(); self.query=QLineEdit(); self.query.setPlaceholderText("粘贴当前页句子查找；重复文字会选中全部匹配区域")
        self.query.returnPressed.connect(self.find_sentence); findrow.addWidget(self.query)
        find=QPushButton("查找句子"); find.clicked.connect(self.find_sentence); findrow.addWidget(find); layout.addLayout(findrow)
        self.query.setPlaceholderText('查找全文句子；可直接选择单个字母或文字')
        exportrow=QHBoxLayout()
        self.export_copy=QPushButton("另存带批注 PDF…"); self.export_copy.clicked.connect(lambda:self.choose_pdf_export(False)); exportrow.addWidget(self.export_copy)
        self.write_original=QPushButton("写回原 PDF…"); self.write_original.clicked.connect(lambda:self.choose_pdf_export(True)); exportrow.addWidget(self.write_original)
        self.cancel_export=QPushButton("取消保存"); self.cancel_export.setEnabled(False); self.cancel_export.clicked.connect(self.cancel_pdf_export); exportrow.addWidget(self.cancel_export)
        exportrow.addStretch(); toolslayout.addLayout(exportrow)
        layout.addWidget(self.tools); self.tools.hide(); self.tools_button.toggled.connect(self.tools.setVisible)
        pages=QWidget(); pagelayout=QVBoxLayout(pages); pagelayout.setContentsMargins(0,0,0,0)
        from .continuous import ContinuousPdfView
        self.pdf_view=ContinuousPdfView(PdfCanvas,self)
        self.pdf_view.selectionChanged.connect(self.selection_changed); self.pdf_view.highlightRequested.connect(self.toggle_highlight); self.pdf_view.zoomRequested.connect(self.adjust_zoom)
        self.pdf_view.currentChanged.connect(self.current_page_changed); self.pdf_view.markSelected.connect(self.select_mark); self.pdf_view.deleteRequested.connect(self.delete_mark)
        self.pdf_view.status.connect(self.status_text); self.pdf_view.error.connect(self.error.emit)
        self.pdf_view.noteStyleChanged.connect(self.save_note_style); self.pdf_view.noteEditRequested.connect(self.edit_page_note)
        self.preload_panel=QWidget(); preload=QHBoxLayout(self.preload_panel); preload.setContentsMargins(0,0,0,0)
        self.preload_progress=QProgressBar(); self.preload_progress.setMaximumHeight(16); preload.addWidget(self.preload_progress,1)
        self.preload_button=QPushButton('暂停预载'); self.preload_button.clicked.connect(self.toggle_preload); preload.addWidget(self.preload_button)
        self.pdf_view.loadingChanged.connect(self.preload_changed); layout.addWidget(self.preload_panel); self.preload_panel.hide()
        self.scroll=self.pdf_view.scroll
        self.scroll.setToolTip('拖选文字或双击句子；Ctrl+滚轮缩放；Shift+滚轮左右滚动；Shift+H高亮/取消高亮')
        self.scroll.setStyleSheet("QScrollArea{background:#e1e7ed;border:1px solid #ccd5dc}")
        self.scroll.viewport().installEventFilter(self)
        pagelayout.addWidget(self.pdf_view)
        self.doc_text=QPlainTextEdit(); self.doc_text.setReadOnly(True); self.doc_text.selectionChanged.connect(lambda:self.selection_changed(self.doc_text.textCursor().selectedText()))
        pagelayout.addWidget(self.doc_text); self.doc_text.hide()
        layout.addWidget(pages,1)
        markpanel=QWidget(); self.mark_panel=markpanel; marklayout=QVBoxLayout(markpanel); marklayout.setContentsMargins(0,0,0,0)
        marklayout.setSpacing(4)
        row=QHBoxLayout(); self.color=QComboBox()
        for key,(name,hexcolor) in COLORS.items(): self.color.addItem(name,key)
        row.addWidget(self.color)
        self.note=QLineEdit(); self.note.setMinimumWidth(60); self.note.setPlaceholderText("可选批注"); row.addWidget(self.note,1)
        self.note.editingFinished.connect(self.save_note_text)
        self.highlight_button=QPushButton(); self.highlight_button.setFixedSize(30,26); self.highlight_button.setIconSize(QSize(22,22)); self.highlight_button.setIcon(icon('highlight'))
        self.highlight_button.setToolTip('高亮（Shift+H）'); self.highlight_button.setAccessibleName('高亮'); self.highlight_button.clicked.connect(self.toggle_highlight); row.addWidget(self.highlight_button)
        self.note_font=QSpinBox(); self.note_font.setRange(6,48); self.note_font.setValue(11); self.note_font.setPrefix('字 ')
        self.note_font.setFixedSize(88,30); self.note_font.setSingleStep(1); self.note_font.setKeyboardTracking(False)
        self.note_font.setStyleSheet('''QSpinBox{padding:2px 24px 2px 3px;font-size:12px}
            QSpinBox::up-button{subcontrol-origin:border;subcontrol-position:top right;width:21px;height:14px;border-left:1px solid #b9c9d2;border-bottom:1px solid #b9c9d2;background:#edf3f7}
            QSpinBox::down-button{subcontrol-origin:border;subcontrol-position:bottom right;width:21px;height:15px;border-left:1px solid #b9c9d2;background:#edf3f7}
            QSpinBox::up-button:hover,QSpinBox::down-button:hover{background:#d1e5ef}
            QSpinBox::up-button:pressed,QSpinBox::down-button:pressed{background:#afcfdf}''')
        self.note_font.setToolTip('选中批注后，点击右上箭头增大、右下箭头减小字号；每次1点，也可直接输入')
        self.note_font.valueChanged.connect(lambda n:self.change_note_font(size=n)); row.addWidget(self.note_font)
        self.note_color=QPushButton(); self.note_color.setIcon(icon('font_color')); self.note_color.setFixedWidth(30); self.note_color.setToolTip('选中批注的字体颜色'); self.note_color.clicked.connect(self.choose_note_color); row.addWidget(self.note_color)
        self.marks_button=QPushButton("批注列表 ▾"); self.marks_button.setCheckable(True); row.addWidget(self.marks_button)
        marklayout.addLayout(row)
        self.mark_details=QWidget(); details=QVBoxLayout(self.mark_details); details.setContentsMargins(0,0,0,0); details.setSpacing(4)
        actions=QHBoxLayout()
        for label,callback in [("修改批注",self.edit_mark)]:
            button=QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(); details.addLayout(actions)
        self.marks=QTreeWidget(); self.marks.setHeaderLabels(["位置 / 颜色","高亮原文","批注"]); self.marks.setColumnWidth(0,130); self.marks.setColumnWidth(1,270)
        self.marks.setMinimumHeight(65); self.marks.setMaximumHeight(120)
        self.marks.setItemDelegate(InlineDelegate(self.marks)); self.marks.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.marks.itemDoubleClicked.connect(self.mark_double_clicked); self.marks.itemChanged.connect(self.mark_note_changed); self.marks.itemSelectionChanged.connect(self.selected_mark_changed)
        self.marks.installEventFilter(self); self.doc_text.installEventFilter(self); details.addWidget(self.marks)
        marklayout.addWidget(self.mark_details); self.mark_details.hide(); self.marks_button.toggled.connect(self.mark_details.setVisible)
        layout.addWidget(markpanel)
        self.status=StatusLabel("高亮先存入数据库；需要在其他阅读器查看时，另存或写回PDF。")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.status.setFixedHeight(20); self.status.setToolTip(self.status.text()); layout.addWidget(self.status)
        self.fit_timer=QTimer(self); self.fit_timer.setSingleShot(True); self.fit_timer.timeout.connect(self.render_pdf)
        self.shortcuts=[]
        for sequence,callback in [("Ctrl++",lambda:self.adjust_zoom(25)),("Ctrl+=",lambda:self.adjust_zoom(25)),("Ctrl+-",lambda:self.adjust_zoom(-25)),("Ctrl+0",self.fit_width)]:
            shortcut=QShortcut(QKeySequence(sequence),self); shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); shortcut.activated.connect(callback); self.shortcuts.append(shortcut)
        QApplication.instance().installEventFilter(self)
        self.selected_mark_changed()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.KeyPress and isinstance(watched,QWidget) and (watched is self or self.isAncestorOf(watched)):
            focus=QApplication.focusWidget()
            typing=isinstance(focus,(QLineEdit,QPlainTextEdit,QTextEdit)) and not focus.isReadOnly()
            if not typing and event.key()==Qt.Key.Key_H and event.modifiers()==Qt.KeyboardModifier.ShiftModifier:
                self.toggle_highlight(); return True
        if event.type()==QEvent.Type.Wheel and isinstance(watched,QWidget) and (watched is self.scroll.viewport() or self.pdf_view.isAncestorOf(watched)):
            if event.modifiers()&Qt.KeyboardModifier.ShiftModifier:
                delta=event.pixelDelta().x() or event.pixelDelta().y() or event.angleDelta().x() or event.angleDelta().y()
                self.pdf_view.horizontal_scroll(delta); return True
            if watched is self.scroll.viewport() and event.modifiers()&Qt.KeyboardModifier.ControlModifier:
                self.adjust_zoom(10 if event.angleDelta().y()>0 else -10); return True
        if watched is self.scroll.viewport() and event.type()==QEvent.Type.Resize and self.zoom_mode=="fit" and self.kind=="pdf":
            if hasattr(self,"fit_timer"): self.fit_timer.start(150)
        if watched in {self.marks,self.doc_text} and event.type()==QEvent.Type.KeyPress and event.key()==Qt.Key.Key_Delete:
            if watched is self.doc_text or self.marks.state()!=QAbstractItemView.State.EditingState:
                self.delete_mark(); return True
        return super().eventFilter(watched,event)

    def preload_changed(self,done,total,state):
        self.preload_panel.setVisible(state not in {'ready','empty'})
        self.preload_progress.setRange(0,total); self.preload_progress.setValue(done)
        self.preload_progress.setFormat(f'全文预加载 {done}/{total}' if total else '正在读取全文信息…')
        self.preload_button.setText('暂停预载' if state=='loading' else '继续预载' if state=='paused' else '重试预载')

    def toggle_preload(self):
        if self.pdf_view.loading: self.pdf_view.pause_loading()
        else: self.pdf_view.resume_loading()

    @property
    def canvas(self): return self.pdf_view.canvas
    @property
    def active_worker(self): return self.pdf_view.worker
    @property
    def pending(self): return self.pdf_view.pending

    def status_text(self,text):
        if hasattr(self,'status'): self.status.setText(text)

    def current_page_changed(self,index):
        self.page.blockSignals(True); self.page.setRange(1,max(1,len(self.pdf_view.pages))); self.page.setValue(index+1); self.page.blockSignals(False)
        if self.zoom_mode=='fit' and self.pdf_view.pages:
            effective=round(self.pdf_view.scale_for(self.pdf_view.pages[index].geometry_info)*75)
            self.zoom.blockSignals(True); self.zoom.setValue(max(25,min(400,effective))); self.zoom.blockSignals(False)
        self.sync_zoom_slider()
        self.remember()

    def go_to_page(self,value):
        if self.kind=='pdf': self.pdf_view.navigate(value-1)

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
        self.pdf_view.clear(); self.pdf_view.show(); self.doc_text.hide()
        self.doc_text.clear(); self.marks.clear(); self.query.clear(); self.note.clear()
        self.export_copy.setEnabled(False); self.write_original.setEnabled(False)
        self.label.setText(message); self.status.setText(message)

    def open_file(self,file_id,evidence=None):
        self.file_id=file_id; self.evidence=evidence; self.query.clear(); self.note.clear()
        self.file_picker.blockSignals(True); self.file_picker.setCurrentIndex(self.file_picker.findData(file_id)); self.file_picker.blockSignals(False)
        self.request_serial+=1; self.pdf_view.stop()
        file=self.store.file(file_id); extraction=json.loads(file["extracted"])
        self.kind=Path(file["path"]).suffix.lstrip(".").lower()
        self.label.setText(file["name"]); self.label.setToolTip(file["reason"])
        state=self.store.reader_state(file_id); self.zoom_mode=state["zoom_mode"]
        self.zoom.blockSignals(True); self.zoom.setValue(state["zoom"]); self.zoom.blockSignals(False)
        self.sync_zoom_slider()
        pdf=self.kind=="pdf"
        self.export_copy.setEnabled(pdf and not self.export_worker); self.write_original.setEnabled(pdf and not self.export_worker)
        self.page.setVisible(pdf)
        self.pdf_view.setVisible(pdf); self.doc_text.setVisible(not pdf)
        self.load_marks()
        if pdf:
            page=state["page_index"]+1
            if evidence:
                loc=next((x for x in extraction["locations"] if x["loc_id"]==evidence["loc_id"]),None)
                if loc: page=loc["page_index"]+1
            self.page.blockSignals(True); self.page.setRange(1,max(page,1)); self.page.setValue(page); self.page.blockSignals(False)
            self.pdf_view.open(self.store.path(file_id),file_id,file['sha256'],page-1,self.zoom.value(),self.zoom_mode=='fit',self.store.highlights(file_id),evidence)
        else:
            self.pdf_view.clear()
            self.doc_text.setPlainText("")
            text,self.spans=docx_display(extraction["locations"])
            self.doc_text.setPlainText(text or file["reason"])
            self.apply_docx_zoom(); self.paint_docx_marks()
            if evidence:
                cursor=self.doc_text.document().find("["+evidence["loc_id"]+"]")
                if not cursor.isNull(): self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()
            self.hint.setText("DOCX直接选中文字后点击高亮；位置使用段落/表格行")
            self.status.setText(file["reason"])

    def manual_zoom(self,value):
        self.sync_zoom_slider()
        self.zoom_mode="manual"
        if self.kind=="pdf": self.render_pdf()
        else: self.apply_docx_zoom(); self.remember()

    def sync_zoom_slider(self):
        self.zoom_slider.blockSignals(True); self.zoom_slider.setValue(zoom_slider(self.zoom.value())); self.zoom_slider.blockSignals(False)

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
        self.pdf_view.set_zoom(self.zoom.value(),self.zoom_mode=='fit')
        self.current_page_changed(self.pdf_view.current)
        if query: self.pdf_view.find(query)
        self.remember()

    def find_sentence(self):
        query=self.query.text().strip()
        if not query: return
        if self.kind=="pdf": self.pdf_view.find(query)
        elif self.kind=="docx":
            cursor=self.doc_text.document().find(query)
            if cursor.isNull(): self.status.setText("未匹配到句子，请直接选择原文")
            else: self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()

    def selection_changed(self,text):
        if text: self.status.setText(f"已选 {len(text)} 字符：{text[:140].replace(chr(10),' ')}")
        if self.file_id and text:
            self.select_mark(self.matching_mark() or '')

    def matching_mark(self):
        payload=self.selection_payload()
        if not payload['anchors']: return None
        anchors=payload['anchors']
        if self.kind=='pdf':
            # Compare actual character rectangles: an older highlight can consist
            # of individual words, while a new drag selection spans their gaps.
            pages=self.pdf_view.pages[self.pdf_view.selection_span[0]:self.pdf_view.selection_span[1]+1] if self.pdf_view.selection_span else [self.canvas]
            fine=[]
            for page in pages:
                if not page.data: continue
                chars=page.data.get('characters',[])
                if page.selection is not None:
                    start,end=sorted(page.selection); chars=chars[start:end+1]
                else:
                    ranges=[a['rect'] for a in anchors if a['page_index']==page.data['page_index']]
                    chars=[c for c in chars if any(r[0]-.5<=(c['rect'][0]+c['rect'][2])/2<=r[2]+.5 and r[1]-.5<=(c['rect'][1]+c['rect'][3])/2<=r[3]+.5 for r in ranges)]
                fine.extend({'page_index':page.data['page_index'],'rect':c['rect']} for c in chars if c['text'].strip())
            if fine: anchors=fine
        for mark in self.pdf_view.marks:
            if mark['kind']!=self.kind: continue
            if self.kind=='pdf':
                def covered(a):
                    return any(a['page_index']==b['page_index'] and all([a['rect'][0]>=b['rect'][0]-.8,a['rect'][1]>=b['rect'][1]-.8,a['rect'][2]<=b['rect'][2]+.8,a['rect'][3]<=b['rect'][3]+.8]) for b in mark['anchors'])
            else:
                def covered(a): return any(a['loc_id']==b['loc_id'] and a['start']>=b['start'] and a['end']<=b['end'] for b in mark['anchors'])
            if all(covered(a) for a in anchors): return mark['mark_id']
        return None

    def toggle_highlight(self):
        if self.marks.currentItem(): self.delete_mark()
        else: self.save_selection()

    def selection_payload(self):
        if self.kind=="pdf": return self.pdf_view.selected_payload()
        cursor=self.doc_text.textCursor()
        return docx_selection(self.spans,cursor.selectionStart(),cursor.selectionEnd())

    def copy_selection(self):
        QApplication.clipboard().setText(self.selection_payload()["text"])

    def save_selection(self):
        if self.export_worker:
            self.status.setText("正在保存PDF，请等待结束后修改高亮"); return
        if self.kind not in {"pdf","docx"} or not self.file_id:
            self.error.emit("请先打开可读取的PDF或DOCX"); return
        try:
            payload=self.selection_payload()
            mark_id=self.store.save_highlight(self.file_id,self.kind,payload["text"],payload["anchors"],self.color.currentData(),self.note.text())
            self.note.clear(); self.load_marks(mark_id)
            if self.kind=="pdf":
                for canvas in self.pdf_view.pages: canvas.clear_selection()
                self.pdf_view.selection_span=None
            else: self.paint_docx_marks()
            self.status.setText("高亮与批注已保存；重新打开或移动数据目录后仍保留，原件未修改")
            self.changed.emit(self.file_id)
        except Exception as exc: self.error.emit(str(exc))

    def load_marks(self,select_id=None):
        self.marks.blockSignals(True)
        self.marks.clear()
        marks=self.store.highlights(self.file_id) if self.file_id else []
        if self.file_id: marks=[m for m in marks if m["file_sha256"]==self.store.file(self.file_id)["sha256"]]
        self.pdf_view.set_marks(marks)
        for mark in marks:
            anchor=mark["anchors"][0]
            position=f"实际页 {anchor['page_index']+1}" if mark["kind"]=="pdf" else anchor["loc_id"]
            item=QTreeWidgetItem([position+" / "+COLORS[mark["color"]][0],mark["text"],mark["note"]])
            item.setData(0,Qt.ItemDataRole.UserRole,mark)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable)
            item.setData(2,EDIT_ROLE,{'multiline':True,'file_id':self.file_id})
            for i in range(3): item.setToolTip(i,item.text(i))
            self.marks.addTopLevelItem(item)
            if mark["mark_id"]==select_id: self.marks.setCurrentItem(item)
        self.marks.blockSignals(False)
        self.selected_mark_changed()

    def select_mark(self,mark_id):
        if not mark_id:
            self.marks.setCurrentItem(None); self.marks.clearSelection(); self.selected_mark_changed(); return
        for i in range(self.marks.topLevelItemCount()):
            item=self.marks.topLevelItem(i)
            if item.data(0,Qt.ItemDataRole.UserRole)['mark_id']==mark_id:
                self.marks.setCurrentItem(item); return

    def selected_mark_changed(self):
        item=self.marks.currentItem()
        selected=item.data(0,Qt.ItemDataRole.UserRole)['mark_id'] if item else None
        for page in self.pdf_view.pages: page.selected_mark=selected; page.update()
        self.highlight_button.setIcon(icon('unhighlight' if selected else 'highlight'))
        self.highlight_button.setToolTip(('取消高亮' if selected else '高亮')+'（Shift+H）'); self.highlight_button.setAccessibleName('取消高亮' if selected else '高亮')
        self.note_font.setEnabled(bool(selected) and self.kind=='pdf'); self.note_color.setEnabled(bool(selected) and self.kind=='pdf')
        if item:
            mark=item.data(0,Qt.ItemDataRole.UserRole); self.note.setText(mark['note'])
            self.note_font.blockSignals(True); self.note_font.setValue(round(mark.get('appearance',{}).get('font_size',11))); self.note_font.blockSignals(False)
        elif selected is None and not self.note.hasFocus(): self.note.clear()

    def save_note_style(self,mark_id,appearance):
        if self.export_worker: return
        try:
            self.store.save_note_style(self.file_id,mark_id,appearance); self.load_marks(mark_id); self.changed.emit(self.file_id)
        except Exception as exc: self.error.emit(str(exc))

    def change_note_font(self,color=None,size=None):
        item=self.marks.currentItem()
        if not item or self.kind!='pdf': return
        mark=item.data(0,Qt.ItemDataRole.UserRole)
        for page in self.pdf_view.pages:
            if mark['mark_id'] in page.note_boxes:
                page.note_boxes[mark['mark_id']].set_font(color,size); return

    def choose_note_color(self):
        value=QColorDialog.getColor(QColor('#28586c'),self,'批注字体颜色')
        if value.isValid(): self.change_note_font(color=value.name())

    def save_note_text(self):
        item=self.marks.currentItem()
        if not item or self.export_worker: return
        mark=item.data(0,Qt.ItemDataRole.UserRole)
        if mark['note']==self.note.text(): return
        self.save_existing_note(mark,self.note.text())

    def save_existing_note(self,mark,text):
        try:
            self.store.save_highlight(self.file_id,mark['kind'],mark['text'],mark['anchors'],mark['color'],text,mark['mark_id'])
            self.load_marks(mark['mark_id']); self.changed.emit(self.file_id)
        except Exception as exc: self.error.emit(str(exc))

    def edit_page_note(self,mark_id):
        if self.export_worker: return
        self.select_mark(mark_id); item=self.marks.currentItem()
        if not item: return
        mark=item.data(0,Qt.ItemDataRole.UserRole); text,ok=QInputDialog.getMultiLineText(self,'编辑页内批注','批注内容：',mark['note'])
        if ok: self.save_existing_note(mark,text)

    def mark_double_clicked(self,item,column):
        if column==2: self.marks.editItem(item,2)
        else: self.jump_mark(item)

    def mark_note_changed(self,item,column):
        if column!=2 or self.export_worker: return
        mark=item.data(0,Qt.ItemDataRole.UserRole)
        fid=item.data(2,EDIT_ROLE)['file_id']
        try:
            self.store.save_highlight(fid,mark['kind'],mark['text'],mark['anchors'],mark['color'],item.text(2),mark['mark_id'])
            self.marks.blockSignals(True)
            mark['note']=item.text(2); item.setData(0,Qt.ItemDataRole.UserRole,mark)
            item.setToolTip(2,item.text(2)); self.marks.blockSignals(False); self.changed.emit(fid)
            self.pdf_view.set_marks(self.store.highlights(fid)); self.note.setText(item.text(2))
        except Exception as exc: self.error.emit(str(exc))

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
            self.pdf_view.navigate(anchor['page_index'])
            canvas=self.canvas
            if canvas.data:
                rect=canvas.screen_rect(canvas.display_rect(anchor['rect']))
                self.scroll.ensureVisible(int(canvas.x()+rect.center().x()),int(canvas.y()+rect.center().y()),40,80)
        else:
            span=next((s for s in self.spans if s["loc_id"]==anchor["loc_id"]),None)
            if span:
                cursor=self.doc_text.textCursor(); cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["start"]])); cursor.setPosition(span["start"]+utf16_length(span["text"][:anchor["end"]]),QTextCursor.MoveMode.KeepAnchor)
                self.doc_text.setTextCursor(cursor); self.doc_text.ensureCursorVisible()

    def delete_mark(self):
        if self.export_worker:
            self.status.setText('正在写入PDF，请等待保存完成后删除高亮'); return
        item=self.marks.currentItem()
        if not item: self.status.setText("请点击原文中的高亮，或在批注列表选择一条记录"); return
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
        self.pdf_view.set_notes_enabled(False)
        self.status.setText("正在后台写入标准PDF批注；已有目标文件会先自动备份…")
        worker.ready.connect(self.pdf_saved)
        worker.failed.connect(lambda message:self.status.setText(message) if worker.cancelled.is_set() else self.error.emit(message))
        def finished():
            self.export_worker=None
            self.export_copy.setEnabled(self.kind=="pdf"); self.write_original.setEnabled(self.kind=="pdf")
            self.cancel_export.setEnabled(False); self.mark_panel.setEnabled(True)
            self.pdf_view.set_notes_enabled(True)
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
        self.request_serial+=1; self.fit_timer.stop(); self.pdf_view.stop()
        if self.export_worker: self.export_worker.cancelled.set()
