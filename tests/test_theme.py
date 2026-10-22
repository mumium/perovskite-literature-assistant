"""Theme switching must preserve the active document and usable controls."""
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QFormLayout,QLineEdit,QComboBox,QCheckBox,QSpinBox,QPushButton,QStyle,QStyleOptionSpinBox

from perovskite.gui import Window
from perovskite.selftest import fixtures
from perovskite.store import Store
from perovskite.theme import apply_theme,is_dark
from test_gui import wait_reader,article_row


def test_toolbar_theme_switch_document_layout_and_restart(tmp_path):
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'portable library'
    with Store(root) as store:
        source=fixtures(tmp_path/'synthetic')
        item=store.import_file(source['main'])
    window=Window(root); window.resize(1280,720); window.show(); app.processEvents()
    try:
        window.table.selectRow(article_row(window,item['article_id'])); wait_reader(window,app)
        window.tabs.setCurrentWidget(window.viewer_tab); app.processEvents()
        reader=window.reader
        assert reader.file_picker.width()>=reader.width()-16
        assert reader.fit_button.geometry().center().y()==reader.focus_button.geometry().center().y()==reader.tools_button.geometry().center().y()
        assert reader.file_picker.geometry().bottom()<reader.fit_button.geometry().top()
        assert not any(button.text()=='修改批注' for button in reader.findChildren(QPushButton))
        page=reader.canvas.data['display_list']; original_icon=window.windowIcon().cacheKey()
        button=window.toolbar.widgetForAction(window.theme_action)
        QTest.mouseClick(button,Qt.MouseButton.LeftButton); app.processEvents(); wait_reader(window,app)
        assert is_dark() and window.theme_action.isChecked() and '浅色' in button.toolTip()
        assert window.tabs.currentWidget() is window.viewer_tab and reader.file_id==item['file_id']
        assert reader.canvas.data['display_list'] is page
        assert original_icon==window.windowIcon().cacheKey()
        saved=json.loads((root/'config.json').read_text(encoding='utf-8'))
        assert saved['theme']=='dark' and saved['api_enabled'] is False and 'base_url' in saved
        assert reader.scroll.viewport().height()>=400
        window.close(); app.processEvents()
        window=Window(root); window.show(); app.processEvents()
        assert is_dark() and window.theme_action.isChecked()
        QTest.mouseClick(window.toolbar.widgetForAction(window.theme_action),Qt.MouseButton.LeftButton)
        assert not is_dark() and json.loads((root/'config.json').read_text(encoding='utf-8'))['theme']=='light'
    finally:
        wait_reader(window,app); window.close(); apply_theme(mode='light'); app.processEvents()


def test_dark_dialog_inputs_indicators_spin_and_light_restore(tmp_path):
    app=QApplication.instance() or QApplication([])
    apply_theme(mode='dark')
    dialog=QDialog(); layout=QFormLayout(dialog)
    text=QLineEdit('可编辑文本'); combo=QComboBox(); combo.addItems(['第一项','第二项'])
    spin=QSpinBox(); spin.setRange(1,100); spin.setValue(50); check=QCheckBox('可选功能')
    for label,widget in [('文字',text),('选项',combo),('数值',spin),('',check)]: layout.addRow(label,widget)
    dialog.show(); app.processEvents()
    try:
        assert text.palette().color(QPalette.ColorRole.Base).lightness()<70
        assert text.palette().color(QPalette.ColorRole.Text).lightness()>180
        text.selectAll(); QTest.keyClicks(text,'editable'); assert text.text()=='editable'
        combo.setFocus(); QTest.keyClick(combo,Qt.Key.Key_Down); assert combo.currentIndex()==1
        QTest.mouseClick(check,Qt.MouseButton.LeftButton); assert check.isChecked()
        option=QStyleOptionSpinBox(); spin.initStyleOption(option)
        for control,delta in [(QStyle.SubControl.SC_SpinBoxUp,1),(QStyle.SubControl.SC_SpinBoxDown,-1)]:
            rect=spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,option,control,spin)
            assert rect.width()>=18 and rect.height()>=12
            old=spin.value(); QTest.mouseClick(spin,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,rect.center())
            assert spin.value()==old+delta
        apply_theme(mode='light'); app.processEvents()
        assert text.palette().color(QPalette.ColorRole.Base).lightness()>240
        assert spin.value()==50 and check.isChecked() and text.text()=='editable'
    finally:
        dialog.close(); apply_theme(mode='light'); app.processEvents()
