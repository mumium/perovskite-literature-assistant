"""Write standard, editable PDF annotations without changing library file identities."""
import json
import os
import shutil
import tempfile
import html
from collections import defaultdict
from pathlib import Path

from .extract import PDF_LOCK, digest
from .store import atomic_json, now, uid

COLORS = {"yellow":(1,.85,.30),"green":(.49,.86,.57),"blue":(.47,.79,1),"pink":(1,.61,.77)}

def save_annotated_pdf(store,file_id,target,write_back=False,cancel=lambda:False):
    import fitz
    source=store.path(file_id)
    file=store.file(file_id)
    target=Path(target).resolve()
    if source.suffix.lower()!=".pdf" or target.suffix.lower()!=".pdf":
        raise ValueError("文件写入仅支持PDF；DOCX高亮暂保存在数据库")
    for protected in ["files","migrations","pdf_backups"]:
        if target.is_relative_to((store.root/protected).resolve()):
            raise ValueError("不能覆盖库内原文副本或备份。请选最初导入的原PDF，或另存到其他位置。")
    if digest(source,cancel)!=file["sha256"]:
        raise ValueError("库内原文哈希已变化，停止写入")
    if write_back and not target.is_file():
        raise ValueError("写回原PDF需要选择一个已存在的文件")
    target.parent.mkdir(parents=True,exist_ok=True)
    current_sha=digest(target,cancel) if target.exists() else None
    records=(store.root/"pdf_exports"/file_id).resolve()
    backup_folder=(store.root/"pdf_backups"/file_id).resolve()
    if not records.is_relative_to(store.root) or not backup_folder.is_relative_to(store.root):
        raise ValueError("PDF保存记录或备份目录越界，停止写入")
    if current_sha and current_sha!=file["sha256"]:
        known=records/(current_sha+".json")
        if not known.exists():
            raise ValueError("所选PDF与当前文献不一致，或已被其他软件修改。未覆盖；请另存副本。")
        record=json.loads(known.read_text(encoding="utf-8"))
        if record.get("file_id")!=file_id or record.get("source_sha256")!=file["sha256"] or record.get("output_sha256")!=current_sha:
            raise ValueError("已有PDF的来源记录不匹配，未覆盖")
    marks=store.highlights(file_id)
    if any(m["file_sha256"]!=file["sha256"] for m in marks):
        raise ValueError("部分高亮的来源哈希不匹配，未写入")
    prefix=f"PLA:{file_id}:{file['sha256']}:"
    handle,temporary=tempfile.mkstemp(prefix=".pla-",suffix=".pdf",dir=target.parent)
    os.close(handle)
    stage=Path(temporary)
    backup=None
    try:
        with PDF_LOCK,fitz.open(target if current_sha else source) as doc:
            if doc.needs_pass:
                raise ValueError("加密PDF暂不写入批注")
            if write_back and doc.get_sigflags()>0:
                raise ValueError("带数字签名的PDF不写回，请另存带批注副本")
            # Remove only annotations previously written by this app for this file.
            for page in doc:
                if cancel(): raise InterruptedError("PDF保存已取消，原文件未改写")
                for annotation in list(page.annots() or []):
                    if annotation.info.get("subject","").startswith(prefix):
                        page.delete_annot(annotation)
            count=0; note_count=0
            for mark in marks:
                by_page=defaultdict(list)
                for anchor in mark["anchors"]:
                    if not 0<=anchor["page_index"]<doc.page_count:
                        raise ValueError("高亮页面超出当前PDF范围")
                    by_page[anchor["page_index"]].append(fitz.Rect(anchor["rect"]))
                for page_index,rects in by_page.items():
                    if cancel(): raise InterruptedError("PDF保存已取消，原文件未改写")
                    page=doc[page_index]
                    annotation=page.add_highlight_annot(rects)
                    annotation.set_colors(stroke=COLORS[mark["color"]])
                    annotation.set_opacity(.35)
                    annotation.set_info(title="钙钛矿文献助手",subject=prefix+mark["mark_id"],content=mark["note"] or mark["text"])
                    annotation.update()
                    count+=1
                if mark['note']:
                    from .note_style import default_note_style,validate_note_style
                    first=mark['anchors'][0]; page=doc[first['page_index']]
                    style=mark.get('appearance') or default_note_style(mark,page.rect.width,page.rect.height,list(fitz.Rect(first['rect'])*page.rotation_matrix))
                    validate_note_style(style); page=doc[style['page_index']]
                    display_rect=fitz.Rect(style['rect']) & page.rect
                    rect=display_rect*page.derotation_matrix
                    if rect.is_empty: raise ValueError('页内批注超出PDF页面，未写入')
                    # CJK fallback fonts are bundled with MuPDF; no Office or
                    # separately installed font/runtime is required on the reader.
                    content='<div>'+html.escape(mark['note']).replace('\n','<br>')+'</div>'
                    css=f"font-family: sans-serif; font-size: {style['font_size']}pt; color: {style['font_color']};"
                    note=page.add_freetext_annot(rect,content,richtext=True,style=css,fill_color=(1,.99,.89),border_color=(.8,.77,.6),border_width=.6,rotate=page.rotation)
                    note.set_info(title='钙钛矿文献助手',subject=prefix+mark['mark_id']+':note',content=mark['note'])
                    # add_freetext_annot has already generated the rich-text
                    # appearance. Updating it again from plain /Contents would
                    # discard explicit HTML line breaks in some MuPDF versions.
                    note_count+=1
            doc.save(stage,garbage=3,deflate=True)
        # Reopen the staged PDF and verify every native annotation and comment.
        with PDF_LOCK,fitz.open(stage) as check:
            saved=[a.info for p in check for a in (p.annots() or []) if a.info.get("subject","").startswith(prefix)]
            if len(saved)!=count+note_count:
                raise ValueError("PDF批注写入校验失败，目标未覆盖")
            for mark in marks:
                if not any(a.get("subject")==prefix+mark["mark_id"] and a.get("content")== (mark["note"] or mark["text"]) for a in saved):
                    raise ValueError("PDF批注内容校验失败，目标未覆盖")
                if mark['note'] and not any(a.get('subject')==prefix+mark['mark_id']+':note' and a.get('content')==mark['note'] for a in saved):
                    raise ValueError('PDF页内文字批注校验失败，目标未覆盖')
        output_sha=digest(stage,cancel)
        if cancel(): raise InterruptedError("PDF保存已取消，原文件未改写")
        if current_sha:
            if digest(target,cancel)!=current_sha:
                raise ValueError("目标PDF在保存期间被修改，未覆盖")
            backup=backup_folder/(uid()+".pdf")
            backup.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(target,backup)
            if digest(backup,cancel)!=current_sha:
                raise ValueError("原PDF备份校验失败，未覆盖")
        report={"file_id":file_id,"source_sha256":file["sha256"],"output_sha256":output_sha,"target_name":target.name,
                "annotation_count":count,"note_box_count":note_count,"mark_ids":[m["mark_id"] for m in marks],"backup_path":backup.relative_to(store.root).as_posix() if backup else None,"created":now(),"status":"prepared"}
        record_path=records/(output_sha+".json")
        atomic_json(record_path,report)
        if cancel(): raise InterruptedError("PDF保存已取消，原文件未改写")
        if current_sha and digest(target,cancel)!=current_sha:
            raise ValueError("目标PDF在保存期间被修改，未覆盖")
        os.replace(stage,target)
        report["status"]="saved"
        warnings=[]
        try:
            atomic_json(record_path,report)
            article=store.db.execute("SELECT article_id FROM links WHERE file_id=?",(file_id,)).fetchone()
            if article:
                with store.db:
                    store.history(article["article_id"],"pdf_exports",None,report,"写回原PDF（自动备份）" if write_back else "另存带批注PDF")
        except Exception:
            warnings.append("PDF已保存，但保存记录或修订历史更新失败；请保留备份")
        return {**report,"target":str(target),"backup":str(backup) if backup else None,"warnings":warnings}
    except PermissionError:
        raise ValueError("无法写入目标PDF：请关闭占用该文件的阅读器，并检查目录写入权限；原备份仍保留") from None
    finally:
        stage.unlink(missing_ok=True)
