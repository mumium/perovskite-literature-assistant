import json
import time
from pathlib import Path
import fitz
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from perovskite.store import Store,restore
from perovskite.extract import digest
from perovskite.selftest import fixtures
from perovskite.pdf_annotations import save_annotated_pdf
from perovskite.reader import ReaderPanel

@pytest.fixture
def annotated_library(tmp_path):
    files=fixtures(tmp_path/"源文件")
    # Simulate a pre-existing comment from a different reader.
    with fitz.open(files["main"]) as doc:
        annotation=doc[0].add_text_annot((40,250),"已有阅读器的批注")
        annotation.set_info(title="Other reader",content="已有阅读器的批注")
        doc.saveIncr()
    with Store(tmp_path/"数据目录") as store:
        imported=store.import_file(files["main"])
        fid=imported["file_id"]
        with fitz.open(store.path(fid)) as doc:
            rect=doc[0].search_for("Control sample annealed at 100 C for 10 min.")[0]
        mark=store.save_highlight(fid,"pdf","Control sample annealed at 100 C for 10 min.",[{"page_index":0,"rect":list(rect)}],"green","退火温度待复核：中文批注")
        yield store,fid,mark,files

def native_annotations(path):
    with fitz.open(path) as doc:
        return [a.info for p in doc for a in (p.annots() or [])]

def test_copy_contains_standard_highlight_and_comment(annotated_library,tmp_path):
    store,fid,mark,files=annotated_library
    before=digest(files["main"])
    output=tmp_path/"另存 中文批注.pdf"
    result=save_annotated_pdf(store,fid,output)
    anns=native_annotations(output)
    assert result["annotation_count"]==1 and not result["backup"]
    assert any(a["content"]=="退火温度待复核：中文批注" for a in anns)
    assert any(a["title"]=="Other reader" for a in anns)
    assert digest(files["main"])==before
    assert digest(store.path(fid))==store.file(fid)["sha256"]
    with fitz.open(output) as doc:
        assert doc[0].get_pixmap(annots=True).samples!=doc[0].get_pixmap(annots=False).samples
        highlight=next(a for a in doc[0].annots() if a.type[0]==fitz.PDF_ANNOT_HIGHLIGHT)
        assert highlight.colors["stroke"]==pytest.approx((.49,.86,.57))

def test_write_back_backup_update_and_remove_only_own_marks(annotated_library):
    store,fid,mark,files=annotated_library
    original=digest(files["main"])
    first=save_annotated_pdf(store,fid,files["main"],write_back=True)
    assert digest(first["backup"])==original and digest(files["main"])!=original
    stored=store.highlights(fid)[0]
    store.save_highlight(fid,"pdf",stored["text"],stored["anchors"],"pink","更新后的批注",mark)
    second=save_annotated_pdf(store,fid,files["main"],write_back=True)
    anns=native_annotations(files["main"])
    assert len(anns)==3 and sum(a["content"]=="更新后的批注" for a in anns)==2
    assert second["backup"]!=first["backup"]
    store.delete_highlight(mark,fid)
    save_annotated_pdf(store,fid,files["main"],write_back=True)
    assert [a["title"] for a in native_annotations(files["main"])]==["Other reader"]
    assert digest(store.path(fid))==store.file(fid)["sha256"]

def test_wrong_file_managed_copy_and_external_edit_rejected(annotated_library,tmp_path):
    store,fid,mark,files=annotated_library
    wrong_before=digest(files["si_pdf"])
    with pytest.raises(ValueError,match="不一致"):
        save_annotated_pdf(store,fid,files["si_pdf"],True)
    assert digest(files["si_pdf"])==wrong_before
    with pytest.raises(ValueError,match="不能覆盖"):
        save_annotated_pdf(store,fid,store.path(fid),True)
    result=save_annotated_pdf(store,fid,files["main"],True)
    with fitz.open(files["main"]) as doc:
        doc[0].insert_text((50,300),"Externally changed text")
        doc.saveIncr()
    external=digest(files["main"])
    with pytest.raises(ValueError,match="修改"):
        save_annotated_pdf(store,fid,files["main"],True)
    assert digest(files["main"])==external
    with pytest.raises(ValueError,match="已存在"):
        save_annotated_pdf(store,fid,tmp_path/"不存在.pdf",True)

def test_cancel_and_backup_restore_preserve_export_records(annotated_library,tmp_path):
    store,fid,mark,files=annotated_library
    before=digest(files["main"])
    with pytest.raises(InterruptedError):
        save_annotated_pdf(store,fid,files["main"],True,cancel=lambda:True)
    assert digest(files["main"])==before
    result=save_annotated_pdf(store,fid,files["main"],True)
    archive=tmp_path/"含文件批注的完整备份.zip"; store.backup(archive)
    restore(archive,tmp_path/"恢复目录")
    with Store(tmp_path/"恢复目录") as restored:
        assert list((restored.root/"pdf_backups"/fid).glob("*.pdf"))
        assert list((restored.root/"pdf_exports"/fid).glob("*.json"))
        again=save_annotated_pdf(restored,fid,files["main"],True)
        assert again["annotation_count"]==1

def test_export_background_worker(annotated_library,tmp_path,monkeypatch):
    store,fid,mark,files=annotated_library
    app=QApplication.instance() or QApplication([])
    panel=ReaderPanel(store); panel.file_id=fid; panel.kind="pdf"
    errors=[]; panel.error.connect(errors.append)
    monkeypatch.setattr(QMessageBox,"information",lambda *a,**kw:None)
    output=tmp_path/"后台导出.pdf"
    panel.start_pdf_export(output)
    deadline=time.monotonic()+10
    while panel.export_worker and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.01)
    assert not panel.export_worker and not errors
    assert output.exists() and "PDF已保存" in panel.status.text()
    assert len(native_annotations(output))==3
    panel.close()
