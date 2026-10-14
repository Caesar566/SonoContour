"""Root changes auto-import; restored startup roots remain manual. Synthetic XLSX only."""
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch
import zipfile
import qt
import slicer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'BreastContour'))


def wait(ms=550):
    loop=qt.QEventLoop();qt.QTimer.singleShot(ms,loop.quit);loop.exec()


def workbook(root, uid):
    root.mkdir()
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    xml += '<row r="1"><c r="A1" t="inlineStr"><is><t>阶段三</t></is></c><c r="E1" t="inlineStr"><is><t>备注</t></is></c></row>'
    xml += f'<row r="2"><c r="C2" t="inlineStr"><is><t>{uid}</t></is></c><c r="E2" t="inlineStr"><is><t>1类</t></is></c></row>'
    xml += '</sheetData></worksheet>'
    with zipfile.ZipFile(root/'segmentations.xlsx','w') as output:
        output.writestr('xl/worksheets/sheet1.xml',xml)


def run():
    settings=slicer.app.settings()
    original=settings.value('BreastContour/Root',None)
    try:
        with tempfile.TemporaryDirectory(prefix='auto-import-') as folder:
            roots=[Path(folder)/str(index) for index in range(3)]
            for index,root in enumerate(roots):
                workbook(root,'2.25.'+str(101+index))
            settings.setValue('BreastContour/Root',str(roots[0]))
            widget=slicer.modules.breastcontour.widgetRepresentation().self()
            calls=[];errors=[]
            with patch.object(widget,'select_case',lambda index:calls.append(index)), patch.object(widget,'error',lambda error:errors.append(str(error))):
                wait()
                assert not widget.cases and not calls, 'Startup must not import'
                widget.root_path.currentPath=str(roots[1]);wait()
                assert len(calls)==1 and widget.cases[0].uid=='2.25.102'
                widget.root_path.currentPath=str(roots[1])+'/.';wait()
                assert len(calls)==1, 'Same normalized directory must not reload'
                widget.import_button.click()
                assert len(calls)==2, 'Manual reload remains available'
                widget.root_path.currentPath=str(roots[0])
                widget.root_path.currentPath=str(roots[2]);wait()
                assert len(calls)==3 and widget.cases[0].uid=='2.25.103'
                current=widget.cases[0]
                widget.session.case=current;widget.session.dirty=True
                with patch.object(widget,'confirm_switch',return_value=False):
                    widget.root_path.currentPath=str(roots[1]);wait()
                assert widget.session.case is current and widget.session.dirty
                assert len(calls)==3 and widget.root_key(widget.root_path.currentPath)==widget.root_key(str(roots[2]))
                broken=Path(folder)/'broken';broken.mkdir()
                (broken/'segmentations.xlsx').write_bytes(b'invalid workbook')
                widget.root_path.currentPath=str(broken);wait()
                assert len(errors)==1 and len(calls)==3 and widget.session.case is current
                assert widget.root_key(widget.root_path.currentPath)==widget.root_key(str(roots[2]))
                widget.session.case=None;widget.session.dirty=False
                empty=Path(folder)/'empty';empty.mkdir()
                widget.root_path.currentPath=str(empty);wait()
                assert len(calls)==3 and len(errors)==1
                assert widget.root_path.enabled and widget.import_button.enabled
                widget.root_path.currentPath=str(roots[0])
                widget.import_button.click();wait()
                assert len(calls)==4 and widget.cases[0].uid=='2.25.101', 'Manual import must cancel the pending automatic import'
                widget._auto_import_timer.stop()
        print('AUTO IMPORT TESTS PASSED')
    finally:
        if original is None:
            settings.remove('BreastContour/Root')
        else:
            settings.setValue('BreastContour/Root',original)


try:
    run()
except Exception:
    traceback.print_exc();slicer.util.exit(1)
else:
    slicer.util.exit(0)
