"""Portable PDF comment geometry in rotated display-page points, not pixels."""
import math
import re

def default_note_style(mark,width,height,anchor):
    w=min(185,max(30,width-12)); h=min(100,max(24,height-12))
    x=min(max(6,anchor[2]+8),max(6,width-w-6))
    y=min(max(6,anchor[1]),max(6,height-h-6))
    return {'page_index':mark['anchors'][0]['page_index'],'rect':[x,y,x+w,y+h],'font_size':11,'font_color':'#28586c'}

def validate_note_style(value):
    if value=={}: return value
    if not isinstance(value,dict) or set(value)!={'page_index','rect','font_size','font_color'}: raise ValueError('批注样式字段不完整')
    r=value['rect']; size=value['font_size']
    if type(value['page_index']) is not int or value['page_index']<0: raise ValueError('批注页码无效')
    if not isinstance(r,list) or len(r)!=4 or any(type(n) not in {int,float} or not math.isfinite(n) or not 0<=n<=100000 for n in r) or r[2]-r[0]<10 or r[3]-r[1]<10: raise ValueError('批注位置或尺寸无效')
    if type(size) not in {int,float} or not math.isfinite(size) or not 6<=size<=48: raise ValueError('批注字号应为6–48')
    if not isinstance(value['font_color'],str) or not re.fullmatch('#[0-9a-fA-F]{6}',value['font_color']): raise ValueError('批注字体颜色无效')
    return value
