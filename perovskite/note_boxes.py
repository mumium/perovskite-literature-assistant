"""Movable/resizable page comments, with page-space geometry and vector text."""
import copy
from PySide6.QtCore import Qt,QRectF,Signal
from PySide6.QtGui import QPainter,QColor,QPen,QFont,QTextDocument,QAbstractTextDocumentLayout
from PySide6.QtWidgets import QWidget,QMenu,QColorDialog,QInputDialog

class PageNote(QWidget):
    changed=Signal(str,object)
    selected=Signal(str)
    editRequested=Signal(str)

    def __init__(self,canvas,mark,style):
        super().__init__(canvas)
        self.canvas=canvas; self.mark_id=mark['mark_id']; self.text=mark['note']; self.style=copy.deepcopy(style)
        self.drag=None; self.setMouseTracking(True); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip('拖动批注移动；拖动右下角调整大小；双击编辑；右键设置字体颜色和字号')
        self.sync_geometry(); self.show()

    def sync_geometry(self):
        r=self.canvas.screen_rect(self.style['rect']); self.setGeometry(r.toAlignedRect()); self.update()

    def set_font(self,color=None,size=None):
        if color: self.style['font_color']=color
        if size: self.style['font_size']=size
        self.changed.emit(self.mark_id,copy.deepcopy(self.style)); self.update()

    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        active=self.canvas.selected_mark==self.mark_id
        p.setBrush(QColor(255,253,227,247)); p.setPen(QPen(QColor('#4b91af' if active else '#d4c99b'),1.5 if active else 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.8,.8,-.8,-.8),4,4)
        p.setPen(QColor('#8d8152')); p.drawLine(4,14,self.width()-4,14)
        font=QFont('Microsoft YaHei UI'); font.setPixelSize(10); p.setFont(font); p.drawText(6,11,'批注 · 拖动 / 双击编辑')
        p.save(); p.setClipRect(QRectF(5,18,max(0,self.width()-10),max(0,self.height()-22)))
        p.translate(5,18)
        doc=QTextDocument(); font=QFont('Microsoft YaHei UI')
        font.setPixelSize(max(1,round(self.style['font_size']*self.canvas.width()/self.canvas.data['width'])))
        doc.setDefaultFont(font); doc.setPlainText(self.text); doc.setTextWidth(max(1,self.width()-10)); doc.setDocumentMargin(0)
        context=QAbstractTextDocumentLayout.PaintContext(); context.palette.setColor(context.palette.ColorRole.Text,QColor(self.style['font_color']))
        doc.documentLayout().draw(p,context); p.restore()
        p.setPen(QPen(QColor('#9c946e'),1))
        for n in [4,8,12]: p.drawLine(self.width()-n,self.height()-2,self.width()-2,self.height()-n)

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.setFocus(); self.raise_(); self.selected.emit(self.mark_id)
            self.drag=(event.globalPosition(),copy.deepcopy(self.style['rect']),event.position().x()>self.width()-16 and event.position().y()>self.height()-16)
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.drag and event.buttons()&Qt.MouseButton.LeftButton:
            start,r,resizing=self.drag; delta=event.globalPosition()-start
            sx=self.canvas.width()/self.canvas.data['width']; sy=self.canvas.height()/self.canvas.data['height']
            dx,dy=delta.x()/sx,delta.y()/sy; width,height=self.canvas.data['width'],self.canvas.data['height']
            if resizing:
                r=[r[0],r[1],min(width,max(r[0]+50,r[2]+dx)),min(height,max(r[1]+30,r[3]+dy))]
            else:
                w,h=r[2]-r[0],r[3]-r[1]; x=max(0,min(width-w,r[0]+dx)); y=max(0,min(height-h,r[1]+dy)); r=[x,y,x+w,y+h]
            self.style['rect']=r; self.sync_geometry(); event.accept(); return
        resize=event.position().x()>self.width()-16 and event.position().y()>self.height()-16
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if resize else Qt.CursorShape.SizeAllCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if self.drag:
            changed=self.style['rect']!=self.drag[1]; self.drag=None
            if changed: self.changed.emit(self.mark_id,copy.deepcopy(self.style))
            event.accept(); return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self,event):
        self.drag=None; self.editRequested.emit(self.mark_id); event.accept()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Delete: self.canvas.deleteRequested.emit(); event.accept(); return
        super().keyPressEvent(event)

    def contextMenuEvent(self,event):
        self.selected.emit(self.mark_id)
        menu=QMenu(self); edit=menu.addAction('编辑批注'); color=menu.addAction('字体颜色…'); size=menu.addAction('字体大小…')
        action=menu.exec(event.globalPos())
        if action==edit: self.editRequested.emit(self.mark_id)
        elif action==color:
            value=QColorDialog.getColor(QColor(self.style['font_color']),self,'批注字体颜色')
            if value.isValid(): self.set_font(color=value.name())
        elif action==size:
            value,ok=QInputDialog.getInt(self,'批注字号','字号（PDF点）：',round(self.style['font_size']),6,48)
            if ok: self.set_font(size=value)
