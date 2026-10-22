"""Internal article drag/drop, separate from importing external files."""
import json
from PySide6.QtCore import Qt,QMimeData,Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QTableWidget,QTreeWidget,QAbstractItemView,QStyledItemDelegate,QLineEdit

ARTICLE_MIME='application/x-perovskite-article-ids'
CATEGORY_NAME_ROLE=int(Qt.ItemDataRole.UserRole)+1

class CategoryNameDelegate(QStyledItemDelegate):
    def createEditor(self,parent,option,index):
        if index.data(CATEGORY_NAME_ROLE) is None: return None
        return QLineEdit(parent)
    def setEditorData(self,editor,index):
        editor.setText(index.data(CATEGORY_NAME_ROLE) or ''); editor.selectAll()
    def setModelData(self,editor,model,index):
        # Keep counts out of the editor; the owner validates/persists the raw name
        # and updates this same item, so the editing index is never destroyed.
        self.parent().nameCommitted.emit(index.data(Qt.ItemDataRole.UserRole),editor.text())

def article_mime(root,ids):
    data=QMimeData(); data.setData(ARTICLE_MIME,json.dumps({'data_directory':str(root),'article_ids':ids},ensure_ascii=False).encode('utf-8')); return data

def read_article_mime(data,root):
    if not data.hasFormat(ARTICLE_MIME): return None
    try:
        raw=bytes(data.data(ARTICLE_MIME))
        if len(raw)>1024**2: return None
        value=json.loads(raw.decode('utf-8')); ids=value['article_ids']
        if value['data_directory']!=str(root) or not isinstance(ids,list) or not 0<len(ids)<=10000: return None
        if any(not isinstance(i,str) or len(i)>100 for i in ids): return None
        return list(dict.fromkeys(ids))
    except (ValueError,TypeError,KeyError): return None

class ArticleTable(QTableWidget):
    def __init__(self,root,parent=None):
        super().__init__(0,5,parent); self.library_root=root
        self.setDragEnabled(True); self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
    def selected_article_ids(self):
        return [self.item(index.row(),0).data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedRows()]
    def selection_mime(self): return article_mime(self.library_root,self.selected_article_ids())
    def startDrag(self,actions):
        if not self.selected_article_ids(): return
        drag=QDrag(self); drag.setMimeData(self.selection_mime()); drag.exec(Qt.DropAction.CopyAction)

class CategoryTree(QTreeWidget):
    articlesDropped=Signal(str,object)
    nameCommitted=Signal(str,str)
    def __init__(self,root,parent=None):
        super().__init__(parent); self.library_root=root
        self.setItemDelegate(CategoryNameDelegate(self))
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setAcceptDrops(True); self.viewport().setAcceptDrops(True); self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly); self.setDefaultDropAction(Qt.DropAction.CopyAction)
    def dragEnterEvent(self,event):
        if read_article_mime(event.mimeData(),self.library_root):
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()
    def dragMoveEvent(self,event):
        item=self.itemAt(event.position().toPoint())
        cid=item.data(0,Qt.ItemDataRole.UserRole) if item else None
        if cid and cid!='__unfiled__' and read_article_mime(event.mimeData(),self.library_root):
            self.setCurrentItem(item); event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()
    def dropEvent(self,event):
        item=self.itemAt(event.position().toPoint()); ids=read_article_mime(event.mimeData(),self.library_root)
        cid=item.data(0,Qt.ItemDataRole.UserRole) if item else None
        if cid and cid!='__unfiled__' and ids:
            self.articlesDropped.emit(cid,ids); event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()
