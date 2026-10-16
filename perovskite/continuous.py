"""Continuous PDF pages, complete background preloading and retained page images."""
import math
import threading
from collections import OrderedDict
from PySide6.QtCore import Qt,QThread,Signal,QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget,QVBoxLayout,QScrollArea
from .extract import PDF_LOCK,digest

def pdf_info(path,expected_hash,cancel):
    import fitz
    before=path.stat()
    if digest(path,cancel)!=expected_hash: raise ValueError('附件哈希已变化，请重新导入；未关联到改变后的文件')
    with PDF_LOCK,fitz.open(path) as doc:
        if doc.needs_pass: raise ValueError('加密PDF无法预览')
        pages=[]
        for page in doc:
            if cancel(): raise InterruptedError()
            pages.append({'width':page.rect.width,'height':page.rect.height,'rotation':list(page.rotation_matrix)})
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('PDF在打开时发生变化，请重新导入')
    return {'pages':pages,'stat':(after.st_size,after.st_mtime_ns)}

def pdf_search(path,query,cancel):
    import fitz
    result={}
    with PDF_LOCK,fitz.open(path) as doc:
        for index,page in enumerate(doc):
            if cancel(): raise InterruptedError()
            hits=[{'rect':list(r),'display_rect':list(r*page.rotation_matrix)} for r in page.search_for(query)]
            if hits: result[index]=hits
    return result

class PdfJob(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,function):
        super().__init__(); self.function=function; self.cancelled=threading.Event()
    def run(self):
        try: self.ready.emit(self.function(self.cancelled.is_set))
        except InterruptedError: pass
        except Exception as exc:
            if not self.cancelled.is_set(): self.failed.emit(str(exc))

class PreloadJob(QThread):
    metadata=Signal(object)
    pageReady=Signal(int,object)
    pageFailed=Signal(int,str)
    failed=Signal(str)
    def __init__(self,path,sha,initial,quality,density,budget,info=None,indices=None):
        super().__init__(); self.cancelled=threading.Event()
        self.path,self.sha,self.initial,self.quality,self.density,self.budget=path,sha,initial,quality,density,budget
        self.info,self.indices=info,indices
    def run(self):
        import fitz
        from .reader import load_pdf_page
        try:
            info=self.info or pdf_info(self.path,self.sha,self.cancelled.is_set)
            if not info['pages']: raise ValueError('PDF没有页面')
            area=sum(g['width']*g['height'] for g in info['pages'])
            # RGB images are shared with their canvases, without PNG copies or
            # QPixmap duplication. Leave room for stride/rounding overhead.
            density=min(self.density,math.sqrt(self.budget*.95/(area*3))/(self.quality/75))
            info=dict(info,density=density,quality=self.quality,reduced=density<self.density-.001)
            if self.cancelled.is_set(): return
            self.metadata.emit(info)
            indices=list(range(len(info['pages']))) if self.indices is None else self.indices
            indices=sorted(indices,key=lambda i:(i!=self.initial,i))
            with PDF_LOCK: doc=fitz.open(self.path)
            try:
                for index in indices:
                    if self.cancelled.is_set(): return
                    stat=self.path.stat()
                    if (stat.st_size,stat.st_mtime_ns)!=info['stat']: raise ValueError('附件在阅读期间被修改，请重新导入')
                    try:
                        data=load_pdf_page(self.path,index,self.quality,pixel_ratio=density,cancel=self.cancelled.is_set,document=doc,raw_image=True)
                        self.pageReady.emit(index,data)
                    except InterruptedError: return
                    except Exception as exc: self.pageFailed.emit(index,str(exc))
            finally:
                with PDF_LOCK: doc.close()
        except InterruptedError: pass
        except Exception as exc:
            if not self.cancelled.is_set(): self.failed.emit(str(exc))

class ContinuousPdfView(QWidget):
    currentChanged=Signal(int)
    selectionChanged=Signal(str)
    highlightRequested=Signal()
    markSelected=Signal(str)
    deleteRequested=Signal()
    zoomRequested=Signal(int)
    status=Signal(str)
    error=Signal(str)
    loadingChanged=Signal(int,int,str)
    def __init__(self,canvas_factory,parent=None):
        super().__init__(parent)
        self.factory=canvas_factory; self.pages=[]; self.info=None; self.file_id=None
        self.worker=None; self.serial=0; self.jobs=[]; self.current=0; self.active_canvas=None
        self.cache=OrderedDict(); self.cache_bytes=0; self.cache_limit=512*1024**2
        self.zoom=100; self.fit=False; self.marks=[]; self.query=''; self.hits={}; self.evidence=None
        self.render_count=0; self.failed_pages=set(); self._layout_changing=False
        self.stopped=True; self.preload_request=None; self.full_ready=False; self.paused=False; self.loading=False; self.load_error=None
        self.selection_span=None
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.scroll=QScrollArea(); self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop)
        self.scroll.setStyleSheet('QScrollArea{background:#e1e7ed;border:1px solid #ccd5dc}')
        self.document=QWidget(); self.document.setStyleSheet('background:#e1e7ed')
        self.page_layout=QVBoxLayout(self.document); self.page_layout.setContentsMargins(8,8,8,8); self.page_layout.setSpacing(12)
        self.scroll.setWidget(self.document); layout.addWidget(self.scroll)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.request_visible)
        self.empty=self.factory(); self.empty.setFixedSize(1,1)

    @property
    def canvas(self): return self.active_canvas or self.empty
    @property
    def pending(self): return self.jobs or self.preload_request or self.timer.isActive()

    def stop(self):
        self.stopped=True; self.serial+=1; self.jobs.clear(); self.timer.stop(); self.preload_request=None; self.loading=False
        if self.worker: self.worker.cancelled.set()

    def clear(self):
        self.stop(); self.info=None; self.file_id=None; self.active_canvas=None; self.selection_span=None
        for page in self.pages:
            self.page_layout.removeWidget(page); page.release_bitmap(True); page.deleteLater()
        self.pages=[]; self.cache.clear(); self.cache_bytes=0; self.full_ready=False
        self.document.setFixedSize(1,1); self.loadingChanged.emit(0,0,'empty')

    def open(self,path,file_id,expected_hash,page_index,zoom,fit,marks,evidence=None):
        self.clear(); self.path=path; self.expected_hash=expected_hash; self.file_id=file_id; self.zoom=zoom; self.fit=fit
        self.stopped=False; self.paused=False; self.load_error=None; self.render_count=0
        self.current=page_index; self.marks=marks; self.evidence=evidence; self.hits={}; self.query=''; self.failed_pages=set()
        self.quality=max(100,min(400,math.ceil(zoom/100)*100))
        self.preload_request=(None,None); self.start_job()

    def start_job(self):
        if self.worker or self.stopped: return
        serial=self.serial; fid=self.file_id
        if self.preload_request is not None:
            info,indices=self.preload_request; self.preload_request=None; self.loading=True; self.paused=False
            worker=PreloadJob(self.path,self.expected_hash,self.current,self.quality,max(2,self.devicePixelRatioF()),self.cache_limit,info,indices)
            self.worker=worker; self.loadingChanged.emit(len(self.cache),len(self.pages),'loading')
            def metadata(result):
                if serial!=self.serial or fid!=self.file_id: return
                self.info=result
                if not self.pages:
                    for i,geometry in enumerate(result['pages']):
                        canvas=self.factory(); canvas.geometry_info=geometry; canvas.page_index=i
                        canvas.document_payload=self.selected_payload
                        canvas.selectionChanged.connect(lambda text,c=canvas:self.selected(c,text))
                        canvas.highlightRequested.connect(self.highlightRequested.emit); canvas.zoomRequested.connect(self.zoomRequested.emit)
                        canvas.markSelected.connect(self.markSelected.emit); canvas.deleteRequested.connect(self.deleteRequested.emit)
                        canvas.dragRequested.connect(lambda start,point,c=canvas:self.drag_across(c,start,point))
                        canvas.setText(f'实际页 {i+1} · 等待全文预加载')
                        self.page_layout.addWidget(canvas,0,Qt.AlignmentFlag.AlignHCenter); self.pages.append(canvas); canvas.show()
                    self.current=max(0,min(self.current,len(self.pages)-1)); self.set_zoom(self.zoom,self.fit,False); self.navigate(self.current)
                    if self.evidence: self.find(self.evidence['quote'],evidence=True)
                self.report_status()
            def page_ready(index,data):
                if serial!=self.serial or fid!=self.file_id: return
                key=(fid,index,self.quality); size=data['image'].sizeInBytes()
                if key in self.cache: self.cache_bytes-=self.cache.pop(key)[1]
                self.cache[key]=(data,size); self.cache_bytes+=size; self.render_count+=1; self.failed_pages.discard(index)
                self.show_page(index,data); self.loadingChanged.emit(len(self.cache),len(self.pages),'loading'); self.report_status()
            def page_failed(index,message):
                if serial!=self.serial: return
                self.failed_pages.add(index); self.pages[index].setText(f'第 {index+1} 页加载失败：{message}'); self.report_status()
            worker.metadata.connect(metadata); worker.pageReady.connect(page_ready); worker.pageFailed.connect(page_failed)
        elif self.jobs:
            function=self.jobs.pop(0); worker=PdfJob(function); self.worker=worker
            def ready(result):
                if serial!=self.serial or fid!=self.file_id: return
                self.hits=result
                for index,page in enumerate(self.pages):
                    if page.data: self.show_page(index,page.data)
                if result:
                    target=next((i for i in sorted(result) if i>=self.current),min(result))
                    if self.evidence: target=next((i for i in result if self.evidence['loc_id'].startswith(f'pdf:p{i}:')),target)
                    self.navigate(target); self.status.emit(f'找到 {sum(len(v) for v in result.values())} 个文字区域；已跳转到实际页 {target+1}')
                else: self.status.emit('全文未匹配，请缩短句子或直接拖选字符')
            worker.ready.connect(ready)
        else: return
        def failed(message):
            if serial!=self.serial: return
            self.load_error=message; self.status.emit(message); self.error.emit(message)
        def finish():
            self.worker=None; worker.deleteLater()
            if serial==self.serial and isinstance(worker,PreloadJob):
                self.loading=False; self.full_ready=bool(self.pages) and all(p.data for p in self.pages) and not self.load_error
                state='ready' if self.full_ready else 'paused' if self.paused else 'failed'
                self.loadingChanged.emit(len(self.cache),len(self.pages),state); self.report_status()
            self.start_job()
        worker.failed.connect(failed); worker.finished.connect(finish); worker.start()

    def pause_loading(self):
        if self.loading and self.worker:
            self.paused=True; self.worker.cancelled.set()

    def resume_loading(self):
        if self.worker or self.full_ready or self.stopped: return
        self.load_error=None; self.paused=False
        missing=[i for i,p in enumerate(self.pages) if not p.data] if self.info else None
        self.preload_request=(self.info,missing); self.start_job()

    def scale_for(self,geometry):
        return max(.05,(self.scroll.viewport().width()-20)/geometry['width']) if self.fit else self.zoom/75

    def set_zoom(self,zoom,fit=False,preserve=True):
        self.zoom=zoom; self.fit=fit
        if not self.info: return
        anchor=self.current
        old=self.pages[anchor] if self.pages else None
        fraction=(self.scroll.verticalScrollBar().value()-old.y())/max(1,old.height()) if old else 0
        self._layout_changing=True
        for page in self.pages:
            scale=self.scale_for(page.geometry_info)
            page.set_display_scale(scale)
        self.document.setFixedSize(max(p.width() for p in self.pages)+16,sum(p.height() for p in self.pages)+12*max(0,len(self.pages)-1)+16)
        self.page_layout.invalidate()
        self.page_layout.activate()
        if preserve and self.pages:
            self.scroll.verticalScrollBar().setValue(round(self.pages[anchor].y()+fraction*self.pages[anchor].height()))
        self._layout_changing=False
        self.report_status()

    def navigate(self,index):
        if not self.pages: return
        index=max(0,min(index,len(self.pages)-1)); self.current=index; self.active_canvas=self.pages[index]
        self.scroll.verticalScrollBar().setValue(max(0,self.pages[index].y()-8))
        self.currentChanged.emit(index); self.report_status()

    def selected(self,canvas,text):
        self.selection_span=None
        self.active_canvas=canvas; self.current=canvas.page_index
        for other in self.pages:
            if other is not canvas: other.clear_selection()
        self.currentChanged.emit(self.current); self.selectionChanged.emit(text)

    def drag_across(self,origin,start,global_point):
        position=self.document.mapFromGlobal(global_point)
        index=next((i for i,p in enumerate(self.pages) if p.y()+p.height()>position.y()),len(self.pages)-1)
        target=self.pages[index]
        if not target.data:
            self.scroll.ensureVisible(position.x(),position.y(),24,40); self.timer.start(1); return
        local=target.mapFromGlobal(global_point)
        end=target.char_at(local)
        if end is None:
            end=0 if local.y()<target.height()/2 else len(target.data.get('characters',[]))-1
        if end<0: return
        self.scroll.ensureVisible(position.x(),position.y(),24,40)
        first,last=sorted([(origin.page_index,start),(index,end)])
        self.selection_span=(first[0],last[0]); self.active_canvas=origin
        for i,page in enumerate(self.pages):
            if first[0]<=i<=last[0] and page.data and page.data.get('characters'):
                page.selection=(first[1] if i==first[0] else 0,last[1] if i==last[0] else len(page.data['characters'])-1)
                page.manual_selection=None; page.update()
            else: page.clear_selection()
        self.selectionChanged.emit(self.selected_payload()['text'])

    def selected_payload(self):
        if self.selection_span:
            pieces=[p.selected_payload() for p in self.pages[self.selection_span[0]:self.selection_span[1]+1]]
            return {'text':'\n'.join(p['text'] for p in pieces if p['text']),'anchors':[a for p in pieces for a in p['anchors']]}
        return self.canvas.selected_payload()

    def scrolled(self,*args):
        if self._layout_changing or not self.pages: return
        y=self.scroll.verticalScrollBar().value()+min(60,self.scroll.viewport().height()//5)
        index=next((i for i,p in enumerate(self.pages) if p.y()+p.height()>y),len(self.pages)-1)
        if index==self.current: return
        self.current=index; self.active_canvas=self.pages[index]; self.currentChanged.emit(index)
        self.report_status()

    def visible_indices(self):
        top=self.scroll.verticalScrollBar().value(); bottom=top+self.scroll.viewport().height()
        return [i for i,p in enumerate(self.pages) if p.y()<bottom and p.y()+p.height()>top] or [self.current]

    def cached(self,index,quality):
        candidates=[key for key in self.cache if key[:2]==(self.file_id,index) and key[2]>=quality]
        if not candidates: return None
        key=min(candidates,key=lambda k:k[2]); value=self.cache[key][0]; self.cache.move_to_end(key); return value

    def request_visible(self):
        self.report_status()

    def show_page(self,index,data):
        page=self.pages[index]; active=page.selection; manual=page.manual_selection
        display=dict(data)
        display['query']=self.query if index in self.hits else ''
        display['hits']=self.hits.get(index,[])
        display['evidence_rects']=[h['display_rect'] for h in self.hits.get(index,[])] if self.evidence else []
        page.blockSignals(True)
        page.set_page(display,self.marks,self.file_id); page.set_display_scale(self.scale_for(page.geometry_info))
        page.blockSignals(False)
        if active is not None or manual is not None: page.selection=active; page.manual_selection=manual

    def set_marks(self,marks):
        self.marks=marks
        for page in self.pages: page.marks=marks; page.update()

    def find(self,query,evidence=False):
        self.query=query
        for page in self.pages: page.clear_selection()
        if not evidence: self.evidence=None
        path=self.path
        self.jobs.append(lambda cancel:pdf_search(path,query,cancel))
        self.start_job()

    def report_status(self):
        if not self.pages: return
        page=self.pages[self.current]; zoom=round(self.scale_for(page.geometry_info)*75)
        text=f'实际页 {self.current+1}/{len(self.pages)} · {zoom}% · 连续滚动 · {len(self.marks)}条高亮'
        if page.data and not page.data['words']: text+=' · 本页无可选文字，扫描页需OCR'
        loaded=len(self.cache)
        text+=f' · 全文已加载 {loaded}/{len(self.pages)}' if self.full_ready else f' · 全文预加载 {loaded}/{len(self.pages)}'
        if self.paused: text+='（已暂停，可继续）'
        if self.failed_pages: text+=f' · {len(self.failed_pages)}页失败，可重试'
        if self.info and self.info.get('reduced'): text+=' · 已按全文内存预算优化分辨率'
        if self.load_error: text+=' · '+self.load_error
        self.status.emit(text)
