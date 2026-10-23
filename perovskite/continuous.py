"""Continuous PDF pages, complete background preloading and retained page images."""
import math
import threading
from collections import OrderedDict
from PySide6.QtCore import Qt,QThread,Signal,QTimer,QPoint
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

def render_detail(display_list,rect,scale,cancel):
    """Replay retained vector commands only for the visible page region."""
    import fitz
    if cancel(): raise InterruptedError()
    with PDF_LOCK:
        pix=display_list.get_pixmap(matrix=fitz.Matrix(scale,scale),colorspace=fitz.csRGB,alpha=False,clip=fitz.Rect(rect))
        image=QImage(pix.samples,pix.width,pix.height,pix.stride,QImage.Format.Format_RGB888).copy()
    if cancel(): raise InterruptedError()
    return {'image':image,'rect':[pix.x/scale,pix.y/scale,(pix.x+pix.width)/scale,(pix.y+pix.height)/scale]}

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
    SESSION_FIELDS=('document','page_layout','pages','info','file_id','path','expected_hash','source_stamp',
        'zoom','fit','quality','marks','hits','query','evidence','current','active_canvas','selection_span',
        'cache','cache_bytes','hd_cache','hd_bytes','render_count','hd_render_count','failed_pages',
        'full_ready','paused','load_error')
    currentChanged=Signal(int)
    selectionChanged=Signal(str)
    highlightRequested=Signal()
    markSelected=Signal(str)
    deleteRequested=Signal()
    zoomRequested=Signal(int)
    status=Signal(str)
    error=Signal(str)
    loadingChanged=Signal(int,int,str)
    noteStyleChanged=Signal(str,object)
    noteEditRequested=Signal(str)
    def __init__(self,canvas_factory,parent=None):
        super().__init__(parent)
        self.factory=canvas_factory; self.pages=[]; self.info=None; self.file_id=None
        self.worker=None; self.serial=0; self.jobs=[]; self.current=0; self.active_canvas=None
        self.cache=OrderedDict(); self.cache_bytes=0; self.cache_limit=512*1024**2
        self.zoom=100; self.fit=False; self.marks=[]; self.query=''; self.hits={}; self.evidence=None
        self.render_count=0; self.failed_pages=set(); self._layout_changing=False
        self.hd_cache=OrderedDict(); self.hd_bytes=0; self.hd_limit=64*1024**2; self.hd_request=None; self.hd_generation=0; self.hd_render_count=0
        self.stopped=True; self.preload_request=None; self.full_ready=False; self.paused=False; self.loading=False; self.load_error=None
        self.selection_span=None
        self.notes_enabled=True
        self.sessions=OrderedDict(); self.session_limit=1024*1024**2; self.session_max_documents=12
        self.session_hits=0; self.cache_notice=''; self._active_key=None
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.scroll=QScrollArea(); self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop)
        self.scroll.setObjectName('pdfScroll')
        self.document=QWidget(); self.document.setObjectName('pdfDocument')
        self.page_layout=QVBoxLayout(self.document); self.page_layout.setContentsMargins(8,8,8,8); self.page_layout.setSpacing(12)
        self.scroll.setWidget(self.document); layout.addWidget(self.scroll)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.scroll.horizontalScrollBar().valueChanged.connect(lambda _:self.timer.start(90) if not self.stopped else None)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.request_visible)
        self.empty=self.factory(); self.empty.setFixedSize(1,1)

    @property
    def canvas(self): return self.active_canvas or self.empty
    @property
    def pending(self): return self.jobs or self.preload_request or self.hd_request or self.timer.isActive()

    def stop(self):
        self.stopped=True; self.serial+=1; self.jobs.clear(); self.timer.stop(); self.preload_request=None; self.loading=False
        self.hd_request=None; self.hd_generation+=1
        if self.worker: self.worker.cancelled.set()

    def clear(self):
        self.suspend(retain=False)
        for session in self.sessions.values(): self._dispose_session(session)
        self.sessions.clear(); self.cache_notice=''

    @staticmethod
    def file_stamp(path):
        stat=path.stat()
        return stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns

    def _dispose_session(self,session):
        for page in session['pages']: page.release_bitmap(True)
        session['cache'].clear(); session['hd_cache'].clear(); session['pages'].clear()
        session['document'].deleteLater()

    def _trim_sessions(self):
        def used(): return self.cache_bytes+self.hd_bytes+sum(s['cache_bytes']+s['hd_bytes'] for s in self.sessions.values())
        while self.sessions and (used()>self.session_limit or len(self.sessions)+bool(self.file_id)>self.session_max_documents):
            _,old=self.sessions.popitem(last=False); self._dispose_session(old)
            self.cache_notice='已释放较早阅读缓存，返回该文献时会重新预载'

    def suspend(self,retain=True):
        """Detach intact page widgets; callbacks from the old job are invalidated."""
        self.stop(); self._layout_changing=True
        scroll_position=(self.scroll.horizontalScrollBar().value(),self.scroll.verticalScrollBar().value())
        viewport_width=self.scroll.viewport().width()
        old=self.scroll.takeWidget()
        if old:
            old.hide(); old.setParent(self)
            session={name:getattr(self,name) for name in self.SESSION_FIELDS if hasattr(self,name)}
            session['scroll']=scroll_position; session['viewport_width']=viewport_width
            session['full_ready']=bool(self.pages) and all(p.data for p in self.pages) and not self.load_error
            if retain and self._active_key and self.info:
                previous=self.sessions.pop(self._active_key,None)
                if previous: self._dispose_session(previous)
                self.sessions[self._active_key]=session
            else: self._dispose_session(session)
        self.pages=[]; self.info=None; self.file_id=None; self.active_canvas=None; self.selection_span=None
        self.cache=OrderedDict(); self.cache_bytes=0; self.hd_cache=OrderedDict(); self.hd_bytes=0
        self.full_ready=False; self.paused=False; self.load_error=None; self._active_key=None
        self.current=0; self.marks=[]; self.hits={}; self.query=''; self.evidence=None
        self.document=QWidget(); self.document.setObjectName('pdfDocument'); self.document.setFixedSize(1,1)
        self.page_layout=QVBoxLayout(self.document); self.page_layout.setContentsMargins(8,8,8,8); self.page_layout.setSpacing(12)
        self.scroll.setWidget(self.document); self._layout_changing=False
        self._trim_sessions(); self.loadingChanged.emit(0,0,'empty')

    def retain_current(self):
        self.suspend()

    def _restore_session(self,key,session,zoom,fit,marks,evidence):
        self._layout_changing=True
        blank=self.scroll.takeWidget()
        if blank: blank.deleteLater()
        for name in self.SESSION_FIELDS: setattr(self,name,session[name])
        self._active_key=key; self.stopped=False; self.session_hits+=1
        self.scroll.setWidget(self.document); self.document.show(); self.page_layout.activate()
        self._layout_changing=False
        if self.zoom!=zoom or self.fit!=fit or (fit and session['viewport_width']!=self.scroll.viewport().width()):
            self.set_zoom(zoom,fit,False)
        self._layout_changing=True
        self.scroll.horizontalScrollBar().setValue(session['scroll'][0]); self.scroll.verticalScrollBar().setValue(session['scroll'][1])
        self._layout_changing=False
        self.set_marks(marks); self.set_notes_enabled(self.notes_enabled)
        self.currentChanged.emit(self.current)
        if evidence:
            self.evidence=evidence; self.find(evidence['quote'],evidence=True)
        if not self.full_ready and not self.paused and not self.load_error:
            missing=[i for i,p in enumerate(self.pages) if not p.data]
            self.preload_request=(self.info,missing); self.start_job()
        else:
            state='ready' if self.full_ready else 'paused' if self.paused else 'failed'
            self.loadingChanged.emit(len(self.cache),len(self.pages),state)
        self._trim_sessions(); self.report_status()
        return True

    def open(self,path,file_id,expected_hash,page_index,zoom,fit,marks,evidence=None):
        self.retain_current()
        key=(str(path.resolve()),file_id,expected_hash)
        session=self.sessions.pop(key,None)
        try: stamp=self.file_stamp(path)
        except OSError:
            if session: self._dispose_session(session)
            raise
        if session:
            if session['source_stamp']==stamp:
                return self._restore_session(key,session,zoom,fit,marks,evidence)
            self._dispose_session(session)
        # Changed hashes/paths must not leave an obsolete copy under the same ID.
        for previous in list(self.sessions):
            if previous[1]==file_id: self._dispose_session(self.sessions.pop(previous))
        self.path=path; self.expected_hash=expected_hash; self.file_id=file_id; self.zoom=zoom; self.fit=fit
        self._active_key=key; self.source_stamp=stamp
        self._trim_sessions()
        self.stopped=False; self.paused=False; self.load_error=None; self.render_count=0
        self.hd_render_count=0
        self.current=page_index; self.marks=marks; self.evidence=evidence; self.hits={}; self.query=''; self.failed_pages=set()
        self.quality=max(100,min(400,math.ceil(zoom/100)*100))
        self.preload_request=(None,None); self.start_job()
        return False

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
                        canvas.notes_editable=self.notes_enabled
                        canvas.selectionChanged.connect(lambda text,c=canvas:self.selected(c,text))
                        canvas.highlightRequested.connect(self.highlightRequested.emit); canvas.zoomRequested.connect(self.zoomRequested.emit)
                        canvas.markSelected.connect(self.markSelected.emit); canvas.deleteRequested.connect(self.deleteRequested.emit)
                        canvas.dragRequested.connect(lambda start,point,c=canvas:self.drag_across(c,start,point))
                        canvas.horizontalRequested.connect(self.horizontal_scroll)
                        canvas.noteStyleChanged.connect(self.noteStyleChanged.emit); canvas.noteEditRequested.connect(self.noteEditRequested.emit)
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
                self._trim_sessions()
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
        elif self.hd_request:
            targets=self.hd_request; self.hd_request=None; generation=self.hd_generation
            stat=self.info['stat']; path=self.path
            def detail(cancel):
                current=path.stat()
                if (current.st_size,current.st_mtime_ns)!=stat: raise ValueError('附件在阅读期间被修改，请重新导入')
                return [(key,index,render_detail(dl,rect,scale,cancel)) for key,index,dl,rect,scale in targets]
            worker=PdfJob(detail); self.worker=worker
            def detail_ready(results):
                if serial!=self.serial or generation!=self.hd_generation: return
                for key,index,data in results:
                    if key in self.hd_cache: self.hd_bytes-=self.hd_cache.pop(key)[1]
                    size=data['image'].sizeInBytes(); self.hd_cache[key]=(data,size); self.hd_bytes+=size; self.hd_render_count+=1
                    self.pages[index].hd=data; self.pages[index].update()
                while self.hd_bytes>self.hd_limit and self.hd_cache:
                    key,(data,size)=self.hd_cache.popitem(last=False); self.hd_bytes-=size
                    for page in self.pages:
                        if page.hd is data: page.hd=None; page.update()
                self._trim_sessions()
                self.report_status()
            worker.ready.connect(detail_ready)
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
                if self.full_ready: self.timer.start(30)
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
        self.hd_generation+=1; self.hd_request=None; self.timer.start(120)
        self.report_status()

    def navigate(self,index):
        if not self.pages: return
        index=max(0,min(index,len(self.pages)-1)); self.current=index; self.active_canvas=self.pages[index]
        self.scroll.verticalScrollBar().setValue(max(0,self.pages[index].y()-8))
        self.currentChanged.emit(index); self.timer.start(70); self.report_status()

    def horizontal_scroll(self,delta):
        bar=self.scroll.horizontalScrollBar(); bar.setValue(bar.value()-delta)

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
        self.timer.start(90)
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
        if self.stopped or not self.full_ready: return
        targets=[]
        for index in self.visible_indices():
            page=self.pages[index]; data=page.data; scale=self.scale_for(page.geometry_info)
            density=max(1.25,self.devicePixelRatioF()); effective=scale*density
            if data['image'].width()/data['width']>=effective-.01: continue
            local=page.mapFrom(self.scroll.viewport(),QPoint(0,0))
            left=max(0,math.floor((local.x()-64)/scale/32)*32); top=max(0,math.floor((local.y()-64)/scale/32)*32)
            right=min(data['width'],math.ceil((local.x()+self.scroll.viewport().width()+64)/scale/32)*32)
            bottom=min(data['height'],math.ceil((local.y()+self.scroll.viewport().height()+64)/scale/32)*32)
            if right<=left or bottom<=top: continue
            rect=[left,top,right,bottom]
            # A viewport patch is bounded separately from whole-document previews.
            effective=min(effective,math.sqrt(16_000_000/((right-left)*(bottom-top))))
            key=(self.file_id,index,round(effective,4),tuple(rect))
            if key in self.hd_cache:
                patch=self.hd_cache[key][0]; self.hd_cache.move_to_end(key); page.hd=patch; page.update()
            else: targets.append((key,index,data['display_list'],rect,effective))
        self.hd_request=targets or None
        self.start_job()
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
        for page in self.pages: page.marks=marks; page.sync_notes(); page.update()

    def set_notes_enabled(self,enabled):
        self.notes_enabled=enabled
        for page in self.pages:
            page.notes_editable=enabled
            for box in page.note_boxes.values(): box.setEnabled(enabled)

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
        if page.hd: text+=' · 矢量高清区域'
        if self.session_hits: text+=' · 已启用原文切换缓存'
        if self.cache_notice: text+=' · '+self.cache_notice
        self.status.emit(text)
