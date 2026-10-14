"""Continuous PDF pages, visible-page loading and bounded high-resolution cache."""
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

class ContinuousPdfView(QWidget):
    currentChanged=Signal(int)
    selectionChanged=Signal(str)
    highlightRequested=Signal()
    markSelected=Signal(str)
    deleteRequested=Signal()
    zoomRequested=Signal(int)
    status=Signal(str)
    error=Signal(str)
    def __init__(self,canvas_factory,parent=None):
        super().__init__(parent)
        self.factory=canvas_factory; self.pages=[]; self.info=None; self.file_id=None
        self.worker=None; self.serial=0; self.jobs=[]; self.current=0; self.active_canvas=None
        self.cache=OrderedDict(); self.cache_bytes=0; self.cache_limit=128*1024**2
        self.zoom=100; self.fit=False; self.marks=[]; self.query=''; self.hits={}; self.evidence=None
        self.render_count=0; self.failed_pages=set(); self._layout_changing=False
        self.stopped=True
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
    def pending(self): return self.jobs or self.timer.isActive()

    def stop(self):
        self.stopped=True; self.serial+=1; self.jobs.clear(); self.timer.stop()
        if self.worker: self.worker.cancelled.set()

    def clear(self):
        self.stop(); self.info=None; self.file_id=None; self.active_canvas=None; self.selection_span=None
        for page in self.pages: self.page_layout.removeWidget(page); page.deleteLater()
        self.pages=[]; self.document.setFixedSize(1,1)

    def open(self,path,file_id,expected_hash,page_index,zoom,fit,marks,evidence=None):
        self.clear(); self.path=path; self.file_id=file_id; self.zoom=zoom; self.fit=fit
        self.stopped=False
        self.current=page_index; self.marks=marks; self.evidence=evidence; self.hits={}; self.query=''; self.failed_pages=set()
        self.jobs.append(('info',None,None,lambda cancel:pdf_info(path,expected_hash,cancel)))
        self.start_job()

    def start_job(self):
        if self.worker or not self.jobs: return
        kind,index,quality,function=self.jobs.pop(0); serial=self.serial; fid=self.file_id
        worker=PdfJob(function); self.worker=worker
        worker.page_key=(fid,index,quality) if kind=='page' else None
        def ready(result):
            if serial!=self.serial or fid!=self.file_id: return
            if kind=='info':
                self.info=result
                for i,geometry in enumerate(result['pages']):
                    canvas=self.factory(); canvas.geometry_info=geometry; canvas.page_index=i
                    canvas.document_payload=self.selected_payload
                    canvas.selectionChanged.connect(lambda text,c=canvas:self.selected(c,text))
                    canvas.highlightRequested.connect(self.highlightRequested.emit)
                    canvas.zoomRequested.connect(self.zoomRequested.emit)
                    canvas.markSelected.connect(self.markSelected.emit)
                    canvas.deleteRequested.connect(self.deleteRequested.emit)
                    canvas.dragRequested.connect(lambda start,point,c=canvas:self.drag_across(c,start,point))
                    self.page_layout.addWidget(canvas,0,Qt.AlignmentFlag.AlignHCenter); self.pages.append(canvas); canvas.show()
                self.current=max(0,min(self.current,len(self.pages)-1))
                self.set_zoom(self.zoom,self.fit,False); self.navigate(self.current)
                if self.evidence: self.find(self.evidence['quote'],evidence=True)
            elif kind=='page':
                image=QImage.fromData(result['png']); size=image.width()*image.height()*4+len(result['png'])
                key=(fid,index,quality)
                if key in self.cache: self.cache_bytes-=self.cache.pop(key)[1]
                self.cache[key]=(result,size); self.cache_bytes+=size; self.render_count+=1
                self.show_page(index,result)
                self.trim_cache()
                self.report_status()
            elif kind=='search':
                self.hits=result
                if result:
                    target=next((i for i in sorted(result) if i>=self.current),min(result))
                    if self.evidence:
                        target=next((i for i in result if self.evidence['loc_id'].startswith(f'pdf:p{i}:')),target)
                    self.navigate(target)
                    if self.pages[target].data: self.show_page(target,self.pages[target].data)
                    self.status.emit(f'找到 {sum(len(v) for v in result.values())} 个文字区域；已跳转到实际页 {target+1}')
                else: self.status.emit('全文未匹配，请缩短句子或直接拖选字符')
        def failed(message):
            if serial!=self.serial: return
            if index is not None:
                self.failed_pages.add(index); self.pages[index].setText(f'第 {index+1} 页加载失败：{message}')
            self.status.emit(message); self.error.emit(message)
        def finish():
            self.worker=None; worker.deleteLater()
            self.start_job()
            if self.info and not self.jobs and not self.stopped: self.timer.start(30)
        worker.ready.connect(ready); worker.failed.connect(failed); worker.finished.connect(finish); worker.start()

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
        self.timer.start(180); self.report_status()

    def navigate(self,index):
        if not self.pages: return
        index=max(0,min(index,len(self.pages)-1)); self.current=index; self.active_canvas=self.pages[index]
        self.scroll.verticalScrollBar().setValue(max(0,self.pages[index].y()-8))
        self.currentChanged.emit(index); self.timer.start(1)

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
        self.current=index; self.active_canvas=self.pages[index]; self.currentChanged.emit(index)
        self.timer.start(70)

    def visible_indices(self):
        top=self.scroll.verticalScrollBar().value(); bottom=top+self.scroll.viewport().height()
        return [i for i,p in enumerate(self.pages) if p.y()<bottom and p.y()+p.height()>top] or [self.current]

    def cached(self,index,quality):
        candidates=[key for key in self.cache if key[:2]==(self.file_id,index) and key[2]>=quality]
        if not candidates: return None
        key=min(candidates,key=lambda k:k[2]); value=self.cache[key][0]; self.cache.move_to_end(key); return value

    def request_visible(self):
        if not self.info or not self.pages or self.stopped: return
        from .reader import load_pdf_page
        visible=self.visible_indices(); nearby=set(visible)
        for i in visible: nearby.update(n for n in (i-1,i+1) if 0<=n<len(self.pages))
        # Drop obsolete queued render work after a fast scroll/zoom.
        self.jobs=[job for job in self.jobs if job[0]!='page']
        for index in sorted(nearby,key=lambda i:(i not in visible,abs(i-self.current))):
            if index in self.failed_pages: continue
            effective=self.scale_for(self.pages[index].geometry_info)*75
            quality=max(100,min(400,math.ceil(effective/100)*100)) if index in visible else 100
            data=self.cached(index,quality)
            if data:
                if self.pages[index].data is not data or self.pages[index].pixmap() is None or self.pages[index].pixmap().isNull(): self.show_page(index,data)
                continue
            if self.worker and getattr(self.worker,'page_key',None)==(self.file_id,index,quality): continue
            path=self.path; stat=self.info['stat']; density=max(2,self.devicePixelRatioF())
            text_data=self.cached(index,100)
            def render(cancel,index=index,quality=quality,text_data=text_data):
                current=path.stat()
                if (current.st_size,current.st_mtime_ns)!=stat: raise ValueError('附件在阅读期间被修改，请重新导入')
                return load_pdf_page(path,index,quality,pixel_ratio=density,text_data=text_data,cancel=cancel)
            self.jobs.append(('page',index,quality,render))
        for index,page in enumerate(self.pages):
            if index not in nearby: page.release_bitmap()
        self.start_job()

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

    def trim_cache(self):
        while self.cache_bytes>self.cache_limit and len(self.cache)>1:
            key,(data,size)=self.cache.popitem(last=False); self.cache_bytes-=size
            if key[0]==self.file_id and key[1]<len(self.pages):
                page=self.pages[key[1]]
                if key[1] not in self.visible_indices() and not any(k[:2]==key[:2] for k in self.cache): page.release_bitmap(True)

    def find(self,query,evidence=False):
        self.query=query
        for page in self.pages: page.clear_selection()
        if not evidence: self.evidence=None
        path=self.path
        self.jobs.insert(0,('search',None,None,lambda cancel:pdf_search(path,query,cancel)))
        self.start_job()

    def report_status(self):
        if not self.pages: return
        page=self.pages[self.current]; zoom=round(self.scale_for(page.geometry_info)*75)
        text=f'实际页 {self.current+1}/{len(self.pages)} · {zoom}% · 连续滚动 · {len(self.marks)}条高亮'
        if page.data and not page.data['words']: text+=' · 本页无可选文字，扫描页需OCR'
        self.status.emit(text)
