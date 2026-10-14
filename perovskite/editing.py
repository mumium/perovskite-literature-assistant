"""Plain-text inline editors, backed by structured records and revision history."""
import copy
import json
import re
from PySide6.QtCore import Qt,QEvent,Signal
from PySide6.QtWidgets import QStyledItemDelegate,QPlainTextEdit,QLineEdit,QTableWidget,QTableWidgetItem,QTreeWidget,QTreeWidgetItem,QComboBox
from .schema import META_FIELDS,STORY_FIELDS,STORY,PROCESS
from .exchange import evidence_checks

EDIT_ROLE=int(Qt.ItemDataRole.UserRole)+40

def text_value(value):
    if value is None: return "未报告"
    if isinstance(value,list): return "；".join(str(v) for v in value)
    if isinstance(value,(dict,bool)): return json.dumps(value,ensure_ascii=False)
    return str(value)

def parse_value(text,old=None,kind=None):
    text=text.strip()
    if kind=="array" or isinstance(old,list):
        if text.startswith('['):
            result=json.loads(text)
            if not isinstance(result,list) or any(not isinstance(v,str) for v in result): raise ValueError("请输入文本标签/作者，以分号分隔")
            return result
        return [v.strip() for v in re.split(r'[；;\n、]',text) if v.strip()]
    if kind=="boolean" or type(old) is bool:
        if text.lower() in {"true","是","齐全"}: return True
        if text.lower() in {"false","否","不齐全"}: return False
        if text in {"","null","未报告"}: return None
        raise ValueError("请输入true/false或留空")
    if kind=="string": return text
    if text in {"","null","未报告"}: return None
    if kind=='scalar':
        try:
            value=json.loads(text)
            if type(value) in {str,int,float,bool} or value is None: return value
        except ValueError: pass
        return text
    if kind=="number" or type(old) in {int,float}:
        value=json.loads(text)
        if type(value) not in {int,float}: raise ValueError("请输入数值；未知条件请留空")
        return value
    return text

class InlineDelegate(QStyledItemDelegate):
    def createEditor(self,parent,option,index):
        binding=index.data(EDIT_ROLE)
        if not binding: return None
        if binding.get('choices'):
            editor=QComboBox(parent)
            for label,value in binding['choices']: editor.addItem(label,value)
            return editor
        if binding.get("multiline"):
            editor=QPlainTextEdit(parent); editor.setToolTip("直接编辑；Ctrl+Enter保存，Esc取消，离开编辑框也会保存")
            return editor
        return QLineEdit(parent)
    def setEditorData(self,editor,index):
        text=index.data(Qt.ItemDataRole.EditRole) or ""
        if isinstance(editor,QComboBox):
            editor.setCurrentIndex(max(0,editor.findData(index.data(EDIT_ROLE).get('value',text)))); return
        if text in {"未报告","双击填写"}: text=""
        if isinstance(editor,QPlainTextEdit): editor.setPlainText(text)
        else: editor.setText(text); editor.selectAll()
    def setModelData(self,editor,model,index):
        model.setData(index,editor.currentData() if isinstance(editor,QComboBox) else editor.toPlainText() if isinstance(editor,QPlainTextEdit) else editor.text(),Qt.ItemDataRole.EditRole)
    def updateEditorGeometry(self,editor,option,index):
        rect=option.rect
        if isinstance(editor,QPlainTextEdit):
            rect.setHeight(max(90,rect.height()))
            if editor.parentWidget() and rect.bottom()>editor.parentWidget().height(): rect.moveBottom(editor.parentWidget().height())
        editor.setGeometry(rect)
    def eventFilter(self,editor,event):
        if isinstance(editor,QPlainTextEdit) and event.type()==QEvent.Type.KeyPress:
            if event.key() in {Qt.Key.Key_Return,Qt.Key.Key_Enter} and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.commitData.emit(editor); self.closeEditor.emit(editor); return True
        return super().eventFilter(editor,event)

def bind(item,column,article_id,path,multiline=False,kind=None):
    item.setFlags(item.flags()|Qt.ItemFlag.ItemIsEditable)
    item.setData(column,EDIT_ROLE,{"article_id":article_id,"path":path,"multiline":multiline,"kind":kind})

def save_content_edit(store,article_id,section_name,path,text,kind=None):
    from jsonschema import validate
    section=store.section(article_id,section_name)
    content=copy.deepcopy(section['content']) if section else ({k:[] for k in STORY_FIELDS} if section_name=='story' else {"samples":[],"performance":[]})
    node=content
    for part in path[:-1]:
        if section_name=='story' and isinstance(node,list) and part==len(node):
            node.append({'text':'','kind':'ai_inference','evidence_ids':[]})
        node=node[part]
    key=path[-1]
    node[key]=parse_value(text,node[key],kind)
    if section_name=='process' and key in {'original_value','original_unit'} and 'normalized_value' in node:
        node['normalized_value']=None; node['normalized_unit']=None; node['conversion_note']=None
    validate(content,STORY if section_name=='story' else PROCESS)
    # Scientific verification remains separate from freely editable user text.
    # A normalized number still requires an explicit basis for the conversion.
    def check(value):
        if isinstance(value,dict):
            if value.get('normalized_value') is not None and 'original_value' in value:
                if value['original_value'] is None or not value.get('normalized_unit') or not value.get('conversion_note'):
                    raise ValueError("填写标准化数值前，请先填写原文值、标准单位和换算说明")
            for child in value.values(): check(child)
        elif isinstance(value,list):
            for child in value: check(child)
    check(content)
    evidence=section['evidence'] if section else []
    checks=evidence_checks(store,article_id,evidence)
    with store.db: store.save_section(article_id,section_name,content,evidence,checks,"双击区域人工编辑",False)
    return content

def populate_process(window):
    """Editable leaf fields keep each original value/unit and sample separate."""
    aid=window.article_id
    process,performance=window.process,window.performance
    for tree in [process,performance]: tree.blockSignals(True); tree.clear()
    section=window.store.section(aid,'process')
    state='人工已核验' if section and section['human_verified'] else '人工待核验'
    process.headerItem().setText(0,'样品 / 工艺步骤 / 字段（'+state+'）')
    performance.headerItem().setText(0,'样品 / 测量 / 字段（'+state+'）')
    content=section['content'] if section else {'samples':[],'performance':[]}
    action_role=int(Qt.ItemDataRole.UserRole)+41
    def action(parent,text,kind,path=None):
        item=QTreeWidgetItem([text]); item.setData(0,action_role,(kind,path))
        if isinstance(parent,QTreeWidget): parent.addTopLevelItem(item)
        else: parent.addChild(item)
    def field(parent,label,value,path,kind=None,multiline=False,choices=None):
        item=QTreeWidgetItem([label,text_value(value)]); parent.addChild(item)
        bind(item,1,aid,path,multiline,kind)
        if choices:
            binding=item.data(1,EDIT_ROLE); binding['choices']=choices; item.setData(1,EDIT_ROLE,binding)
        item.setToolTip(1,text_value(value)); return item
    labels={'original_value':'原文值','original_unit':'原文单位','normalized_value':'标准化值','normalized_unit':'标准单位','basis':'比例计算基准','conversion_note':'换算说明','conflict_group':'冲突组'}
    def parameter(parent,param,path):
        summary=QTreeWidgetItem([param['name'],text_value(param['original_value'])+' '+(param['original_unit'] or ''),text_value(param['normalized_value'])+' '+(param['normalized_unit'] or ''),'基准：'+(param['basis'] or '未报告'),', '.join(param['evidence_ids'])])
        summary.setData(0,Qt.ItemDataRole.UserRole,param['evidence_ids']); parent.addChild(summary); bind(summary,0,aid,path+['name'],False,'string')
        for key,label in labels.items(): field(summary,label,param[key],path+[key],'number' if key=='normalized_value' else 'scalar' if key=='original_value' else None,key in {'conversion_note','basis'})
    for i,sample in enumerate(content['samples']):
        root=QTreeWidgetItem([sample['label']]); root.setToolTip(0,'样品ID：'+sample['sample_id']); process.addTopLevelItem(root); bind(root,0,aid,['samples',i,'label'],False,'string')
        field(root,'实验组',sample['group'],['samples',i,'group'],'string',choices=[('对照','control'),('浓度组','concentration'),('优化组','optimized'),('其他','other')])
        for j,step in enumerate(sample['steps']):
            path=['samples',i,'steps',j]
            child=QTreeWidgetItem([step['category'],step['description'],'','',', '.join(step['evidence_ids'])]); child.setData(0,Qt.ItemDataRole.UserRole,step['evidence_ids']); root.addChild(child)
            bind(child,0,aid,path+['category'],False,'string'); bind(child,1,aid,path+['description'],True,'string')
            field(child,'步骤顺序',step['order'],path+['order'],'number')
            for k,param in enumerate(step['parameters']): parameter(child,param,path+['parameters',k])
            action(child,'＋ 双击添加参数','parameter',path+['parameters'])
        action(root,'＋ 双击添加工艺步骤','step',['samples',i,'steps'])
    action(process,'＋ 双击添加样品 / 实验组','sample')
    choices=[(s['label'],s['sample_id']) for s in content['samples']]
    for i,record in enumerate(content['performance']):
        path=['performance',i]
        name=next((s['label'] for s in content['samples'] if s['sample_id']==record['sample_id']),record['sample_id'])
        root=QTreeWidgetItem([name,record['measurement_kind'],'','',' ,'.join(record['evidence_ids'])]); root.setData(0,Qt.ItemDataRole.UserRole,record['evidence_ids']); performance.addTopLevelItem(root)
        bind(root,1,aid,path+['measurement_kind'],False,'string')
        field(root,'所属样品',record['sample_id'],path+['sample_id'],'string',choices=choices)
        conditions=QTreeWidgetItem(['测试条件']); root.addChild(conditions)
        for key,value in record['conditions'].items(): field(conditions,key,value,path+['conditions',key],'scalar')
        action(conditions,'＋ 双击添加测试条件','condition',path+['conditions'])
        for j,param in enumerate(record['values']): parameter(root,param,path+['values',j])
        action(root,'＋ 双击添加性能参数','parameter',path+['values'])
    action(performance,'＋ 双击添加性能 / 稳定性记录','performance')
    for tree in [process,performance]: tree.expandAll(); tree.blockSignals(False)
