import pytest
from PySide6.QtWidgets import QApplication
from perovskite.gui import Window

def test_data_directory_lock_and_reopen(tmp_path):
    app = QApplication.instance() or QApplication([])
    first = Window(tmp_path / "锁测试 中文 目录")
    with pytest.raises(ValueError, match="正在使用"):
        Window(tmp_path / "锁测试 中文 目录")
    first.close()
    app.processEvents()
    second = Window(tmp_path / "锁测试 中文 目录")
    assert second.table.rowCount() == 0
    second.reader.focus_button.click()
    assert second._reading_mode and second.center_panel.isHidden()
    second.reader.focus_button.click()
    assert not second._reading_mode and not second.center_panel.isHidden()
    second.close()
