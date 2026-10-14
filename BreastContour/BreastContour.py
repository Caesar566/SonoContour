"""Slicer scripted module: breast ultrasound contour annotation."""
from pathlib import Path
import logging
import os

import ctk
import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import ScriptedLoadableModule, ScriptedLoadableModuleWidget

from BreastContourLib.core import discover_cases
from BreastContourLib.runtime import CaseSession, named_segments


class BreastContour(ScriptedLoadableModule):
    def __init__(self, parent):
        super().__init__(parent)
        parent.title = "乳腺超声轮廓标注"
        parent.categories = ["Segmentation"]
        parent.dependencies = ["Segmentations", "SegmentEditor"]
        parent.contributors = ["BreastContour"]
        parent.helpText = "导入病例根目录，逐帧描绘轮廓。医生原始标注作为独立参考，所有工作帧可编辑；保存为当前病例的 Segment.nrrd。"
        parent.acknowledgementText = "Built with 3D Slicer."


class BreastContourWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        super().setup()
        if not getattr(slicer.modules, "_breastContourSmartRegistered", False):
            import qSlicerSegmentationsEditorEffectsPythonQt as effects
            adapter = effects.qSlicerSegmentEditorScriptedEffect(None)
            # Slicer loads scripted effects under their parent directory name.
            # Keep effects outside BreastContourLib so its package is preserved.
            adapter.setPythonSource(str(Path(__file__).parent / "BreastContourEffects" / "SegmentEditorSmartContourEffect.py").replace("\\", "/"))
            adapter.self().register()
            slicer.modules._breastContourSmartRegistered = True
        self.session = CaseSession(self.on_data_changed)
        self.cases = []
        self.current_index = -1
        self.issues = []
        self._updating = False
        self._brush_busy = False
        self._loading = False
        self._slice_observer = None
        self._wheel_observers = []
        self._wheel_event_tags = {}
        self._brush_observer = None
        self._active = False
        self._frame = 0
        self._last_effect = None
        self._space_smart = False
        self._space_restore_effect = ""
        self.shortcuts = []

        data_group = ctk.ctkCollapsibleButton()
        data_group.text = "病例导入与切换"
        self.layout.addWidget(data_group)
        data_layout = qt.QVBoxLayout(data_group)
        self.root_path = ctk.ctkPathLineEdit()
        self.root_path.filters = ctk.ctkPathLineEdit.Dirs
        default_root = Path(__file__).resolve().parent.parent
        default_path = str(default_root) if (default_root / "segmentations.xlsx").exists() else ""
        self.root_path.currentPath = str(slicer.app.settings().value("BreastContour/Root", default_path))
        data_layout.addWidget(self.root_path)
        self.import_button = qt.QPushButton("导入根目录")
        data_layout.addWidget(self.import_button)
        self.case_combo = qt.QComboBox()
        self.case_combo.minimumContentsLength = 20
        self.case_combo.sizeAdjustPolicy = qt.QComboBox.AdjustToMinimumContentsLengthWithIcon
        data_layout.addWidget(self.case_combo)
        case_buttons = qt.QHBoxLayout()
        self.previous_case = qt.QPushButton("上一例")
        self.next_case = qt.QPushButton("下一例")
        case_buttons.addWidget(self.previous_case)
        case_buttons.addWidget(self.next_case)
        data_layout.addLayout(case_buttons)
        self.note_label = qt.QLabel("请选择含 segmentations.xlsx 的病例根目录。")
        self.note_label.wordWrap = True
        self.note_label.textFormat = qt.Qt.PlainText
        self.note_label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        data_layout.addWidget(self.note_label)

        self.annotation_panel = qt.QWidget()
        self.layout.addWidget(self.annotation_panel)
        panel_layout = qt.QVBoxLayout(self.annotation_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_label = qt.QLabel("尚未加载病例")
        self.frame_label.wordWrap = True
        self.frame_label.textFormat = qt.Qt.PlainText
        panel_layout.addWidget(self.frame_label)
        frame_row = qt.QHBoxLayout()
        self.previous_frame = qt.QPushButton("←")
        self.next_frame = qt.QPushButton("→")
        self.frame_spin = qt.QSpinBox()
        self.frame_spin.setPrefix("帧索引 ")
        frame_row.addWidget(self.previous_frame)
        frame_row.addWidget(self.frame_spin)
        frame_row.addWidget(self.next_frame)
        panel_layout.addLayout(frame_row)
        self.frame_slider = qt.QSlider(qt.Qt.Horizontal)
        panel_layout.addWidget(self.frame_slider)
        self.jump_combo = qt.QComboBox()
        self.jump_combo.addItem("跳转范围端点或医生帧…", -1)
        panel_layout.addWidget(self.jump_combo)

        brush_row = qt.QFormLayout()
        self.paint_size = qt.QDoubleSpinBox()
        self.erase_size = qt.QDoubleSpinBox()
        for widget, effect, default in ((self.paint_size, "Paint", 1.5), (self.erase_size, "Erase", 15)):
            widget.setRange(0.5, 200)
            widget.setDecimals(1)
            widget.setSingleStep(0.1)
            widget.setSuffix(" 像素")
            widget.setValue(float(slicer.app.settings().value("BreastContour/" + effect + "Pixels", default)))
        brush_row.addRow("画笔直径", self.paint_size)
        brush_row.addRow("橡皮直径", self.erase_size)
        self.smart_tolerance = qt.QSpinBox()
        self.smart_tolerance.setRange(1, 100)
        self.smart_tolerance.setValue(int(slicer.app.settings().value("BreastContour/SmartTolerance", 15)))
        self.smart_tolerance.setToolTip("图像中按住空格临时启用智能标注，松开恢复原工具；滚轮调节边界阈值；左键确认；Esc 清除预览")
        self.smart_tolerance.connect("valueChanged(int)", self.smart_tolerance_changed)
        brush_row.addRow("智能轮廓边界容忍度", self.smart_tolerance)
        self.smart_radius = qt.QSpinBox()
        self.smart_radius.setRange(0, 50)
        self.smart_radius.setSuffix(" 像素")
        self.smart_radius.setValue(int(slicer.app.settings().value("BreastContour/SmartRadius", 6)))
        self.smart_radius.setToolTip("参考区域半径：0 为鼠标单像素；6 为 13×13 像素；使用区域偏暗灰度统计，不改变边界容忍度")
        self.smart_radius.connect("valueChanged(int)", self.smart_radius_changed)
        brush_row.addRow("智能轮廓参考区域半径", self.smart_radius)
        panel_layout.addLayout(brush_row)
        self.doctor_visible = qt.QCheckBox("显示医生原始轮廓（半透明参考）")
        self.doctor_visible.setChecked(True)
        self.doctor_visible.connect("toggled(bool)", self.set_doctor_visible)
        panel_layout.addWidget(self.doctor_visible)

        self.editor = slicer.qMRMLSegmentEditorWidget()
        self.empty_editor_node = slicer.vtkMRMLSegmentEditorNode()
        self.editor.setMRMLSegmentEditorNode(self.empty_editor_node)
        self.editor.setMRMLScene(slicer.mrmlScene)
        self.editor.setEffectNameOrder(["Paint", "Erase", "SmartContour"])
        self.editor.unorderedEffectsVisible = False
        self.editor.setSegmentationNodeSelectorVisible(False)
        self.editor.setSourceVolumeNodeSelectorVisible(False)
        self.editor.setAddRemoveSegmentButtonsVisible(False)
        self.editor.setSwitchToSegmentationsButtonVisible(False)
        self.editor.setSpecifyGeometryButtonVisible(False)
        self.editor.setShow3DButtonVisible(False)
        self.editor.setMaskingSectionVisible(False)
        self.editor.setJumpToSelectedSegmentEnabled(False)
        self.editor.setMaximumNumberOfUndoStates(5)
        self.editor.setUndoEnabled(True)

        save_row = qt.QHBoxLayout()
        self.check_button = qt.QPushButton("检查轮廓")
        self.save_button = qt.QPushButton("一键保存 (Ctrl+S)")
        save_row.addWidget(self.check_button)
        save_row.addWidget(self.save_button)
        panel_layout.addLayout(save_row)
        self.status = qt.QLabel("")
        self.status.wordWrap = True
        self.status.textFormat = qt.Qt.PlainText
        panel_layout.addWidget(self.status)
        panel_layout.addWidget(self.editor)
        self.issue_list = qt.QListWidget()
        self.issue_list.minimumHeight = 160
        panel_layout.addWidget(self.issue_list)
        self.layout.addStretch(1)
        self.annotation_panel.enabled = False

        self.import_button.connect("clicked()", self.import_root)
        self.case_combo.connect("currentIndexChanged(int)", self.select_case)
        self.previous_case.connect("clicked()", lambda: self.select_case(self.current_index-1))
        self.next_case.connect("clicked()", lambda: self.select_case(self.current_index+1))
        self.previous_frame.connect("clicked()", lambda: self.go_to_frame(self._frame-1))
        self.next_frame.connect("clicked()", lambda: self.go_to_frame(self._frame+1))
        self.frame_spin.connect("valueChanged(int)", self.go_to_frame)
        self.frame_slider.connect("valueChanged(int)", self.go_to_frame)
        self.jump_combo.connect("activated(int)", self.jump_selected)
        self.paint_size.connect("valueChanged(double)", lambda value: self.size_changed("Paint", value))
        self.erase_size.connect("valueChanged(double)", lambda value: self.size_changed("Erase", value))
        self.check_button.connect("clicked()", self.run_check)
        self.save_button.connect("clicked()", self.save)
        self.issue_list.connect("currentRowChanged(int)", self.issue_selected)
        self._scene_observer = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.StartCloseEvent, self.on_scene_close)
        window = slicer.util.mainWindow()
        if window:
            # WidgetWithChildrenShortcut keeps the standard Slicer save action usable
            # in other modules, while the application event filter routes Ctrl+S here.
            self.save_filter = SaveKeyFilter(self)
            slicer.app.installEventFilter(self.save_filter)

    def enter(self):
        self._active = True
        self.editor.setupViewObservations()
        if self.session.case:
            self.attach_view()
            self.editor.setActiveEffectByName("Paint")

    def exit(self):
        self.end_space_smart()
        self._active = False
        self.editor.setActiveEffectByName("")
        self.editor.removeViewObservations()
        self.clear_overlay()
        if hasattr(self, "slice_widget"):
            self.slice_widget.sliceController().setVisible(True)

    def cleanup(self):
        self.detach()
        self.session.close()
        slicer.mrmlScene.RemoveObserver(self._scene_observer)
        if hasattr(self, "save_filter"):
            slicer.app.removeEventFilter(self.save_filter)

    def error(self, message):
        logging.error(message)
        slicer.util.errorDisplay(str(message), windowTitle="乳腺轮廓标注")

    def confirm_switch(self):
        if not self.session.case or not self.session.dirty:
            return True
        box = qt.QMessageBox(slicer.util.mainWindow())
        box.setWindowTitle("当前病例有未保存修改")
        box.setText("切换前如何处理当前标注？")
        save = box.addButton("保存并切换", qt.QMessageBox.AcceptRole)
        discard = box.addButton("放弃修改", qt.QMessageBox.DestructiveRole)
        box.addButton("取消", qt.QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == save:
            return self.save()
        return box.clickedButton() == discard

    def import_root(self):
        if self._loading:
            return
        try:
            cases = discover_cases(self.root_path.currentPath)
            if not cases:
                raise ValueError("没有找到病例")
            if not self.confirm_switch():
                return
            self.detach()
            self.session.close()
            self.current_index = -1
            self.cases = cases
            self.case_combo.blockSignals(True)
            self.case_combo.clear()
            for case in cases:
                marker = "⚠ " if case.warnings else ""
                resume = " [已有结果]" if case.output.exists() else " [未开始]"
                self.case_combo.addItem(marker + case.uid + resume)
                self.case_combo.setItemData(self.case_combo.count-1, "\n".join(case.warnings), qt.Qt.ToolTipRole)
            self.case_combo.blockSignals(False)
            slicer.app.settings().setValue("BreastContour/Root", self.root_path.currentPath)
            self.select_case(0)
        except Exception as exc:
            self.error(exc)

    def select_case(self, index):
        if self._loading or index < 0 or index >= len(self.cases) or index == self.current_index:
            return
        if not self.confirm_switch():
            self.case_combo.blockSignals(True)
            self.case_combo.setCurrentIndex(self.current_index)
            self.case_combo.blockSignals(False)
            return
        self._loading = True
        self.annotation_panel.enabled = False
        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            self.detach()
            self.session.close()
            self.current_index = -1
            self.session.load(self.cases[index])
            self.current_index = index
            self.case_combo.blockSignals(True)
            self.case_combo.setCurrentIndex(index)
            self.case_combo.blockSignals(False)
            case = self.session.case
            self.case_combo.setItemText(index, ("⚠ " if case.warnings else "") + case.uid + (" [已有结果]" if case.output.exists() else " [未开始]"))
            self.case_combo.setItemData(index, "\n".join(case.warnings), qt.Qt.ToolTipRole)
            self.note_label.text = "原始备注：" + case.note + ("\n提示：" + "；".join(case.warnings) if case.warnings else "")
            self.editor.setMRMLSegmentEditorNode(self.session.editor_node)
            self.editor.setSegmentationNode(self.session.work)
            self.editor.setSourceVolumeNode(self.session.volume)
            self.set_doctor_visible(self.doctor_visible.checked)
            self.editor.setCurrentSegmentID(next(iter(named_segments(self.session.work).values())))
            # Run after the native widget has processed the active-effect change.
            self._brush_observer = self.session.editor_node.AddObserver(vtk.vtkCommand.ModifiedEvent, self.brush_parameters_changed, -1.0)
            self.jump_combo.clear()
            self.jump_combo.addItem("跳转范围端点或医生帧…", -1)
            assigned_doctor_frames = set()
            for number, item in enumerate(case.ranges, 1):
                title = f"肿瘤{number} · {item.category} {item.start}–{item.end}"
                for frame, label in ((item.start, "起始"), (item.end, "结束")):
                    if frame < self.session.shape[0]:
                        self.jump_combo.addItem(f"{title} · {label}：{frame}", frame)
                for frame in self.session.doctor_frames:
                    if item.contains(int(frame)):
                        self.jump_combo.addItem(f"{title} · 医生参考：{int(frame)}", int(frame))
                        assigned_doctor_frames.add(int(frame))
            for frame in self.session.doctor_frames:
                if int(frame) not in assigned_doctor_frames:
                    self.jump_combo.addItem(f"范围外医生参考：{int(frame)}", int(frame))
            self._updating = True
            self.frame_spin.setRange(0, self.session.shape[0]-1)
            self.frame_slider.setRange(0, self.session.shape[0]-1)
            self._updating = False
            self.issues = []
            self.issue_list.clear()
            self.annotation_panel.enabled = True
            self.attach_view()
            self.editor.setActiveEffectByName("Paint")
            self.on_effect_changed()
            self.status.text = f"已加载；医生参考帧 {len(self.session.doctor_frames)} 个，所有工作帧均可编辑。"
        except Exception as exc:
            self.detach()
            self.session.close()
            self.annotation_panel.enabled = False
            self.error(exc)
        finally:
            self._loading = False
            slicer.app.restoreOverrideCursor()

    def detach(self):
        self.end_space_smart()
        self.remove_frame_wheel_observers()
        self.clear_overlay()
        if self._slice_observer:
            self._slice_observer[0].RemoveObserver(self._slice_observer[1])
            self._slice_observer = None
        if self._brush_observer and self.session.editor_node:
            self.session.editor_node.RemoveObserver(self._brush_observer)
            self._brush_observer = None
        self.editor.setActiveEffectByName("")
        self.editor.setSegmentationNode(None)
        self.editor.setSourceVolumeNode(None)
        self.editor.setMRMLSegmentEditorNode(self.empty_editor_node)

    def attach_view(self):
        self.remove_frame_wheel_observers()
        manager = slicer.app.layoutManager()
        manager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.slice_widget = manager.sliceWidget("Red")
        self.slice_widget.sliceController().setVisible(False)
        annotation = self.slice_widget.sliceView().cornerAnnotation()
        annotation.SetMaximumFontSize(18)
        # VTK does not inherit Qt's Chinese fallback fonts. Explicitly provide
        # a Chinese font file on Windows as well as Linux.
        font_candidates = []
        if os.name == "nt":
            windows_fonts = Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows") / "Fonts"
            font_names = ("simhei.ttf", "msyh.ttc", "msyh.ttf", "simsun.ttc", "Deng.ttf")
            font_candidates.extend(windows_fonts / name for name in font_names)
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                user_fonts = Path(local_app_data) / "Microsoft" / "Windows" / "Fonts"
                font_candidates.extend(user_fonts / name for name in font_names)
        font_candidates.extend(Path(font) for font in (
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc"))
        for font in font_candidates:
            if font.is_file():
                annotation.GetTextProperty().SetFontFamily(vtk.VTK_FONT_FILE)
                annotation.GetTextProperty().SetFontFile(str(font))
                break
        slicer.util.setSliceViewerLayers(background=self.session.volume)
        node = self.slice_widget.mrmlSliceNode()
        matrix = vtk.vtkMatrix4x4()
        self.session.volume.GetIJKToRASMatrix(matrix)
        normal = [matrix.GetElement(i, 2) / self.session.volume.GetSpacing()[2] for i in range(3)]
        transverse = [matrix.GetElement(i, 0) / self.session.volume.GetSpacing()[0] for i in range(3)]
        center = matrix.MultiplyPoint(((self.session.shape[2]-1)/2, (self.session.shape[1]-1)/2, 0, 1))
        self._updating = True
        node.SetSliceToRASByNTP(*(normal + transverse + list(center[:3]) + [0]))
        # Reverse screen-up while keeping screen-right fixed. Reverse the
        # viewing normal too to retain an orthonormal slice coordinate frame.
        # Only the view changes; image/segmentation IJK geometry stays intact.
        orientation = node.GetSliceToRAS()
        for row in range(3):
            for column in (1, 2):
                orientation.SetElement(row, column, -orientation.GetElement(row, column))
        node.UpdateMatrices()
        self._plane = [[node.GetSliceToRAS().GetElement(i, j) for j in range(3)] for i in range(3)]
        self._updating = False
        self.slice_widget.sliceLogic().FitSliceToAll()
        if self._slice_observer:
            self._slice_observer[0].RemoveObserver(self._slice_observer[1])
        self._slice_observer = (node, node.AddObserver(vtk.vtkCommand.ModifiedEvent, self.on_slice_changed))
        interactor = self.slice_widget.sliceView().interactor()
        for event in ("MouseWheelForwardEvent", "MouseWheelBackwardEvent"):
            tag = interactor.AddObserver(event, self.on_frame_wheel, 10.0)
            self._wheel_observers.append((interactor, tag))
            self._wheel_event_tags[event] = tag
        target = min((r.start for r in self.session.case.ranges if r.start < self.session.shape[0]), default=0)
        self.go_to_frame(target)

    def remove_frame_wheel_observers(self):
        for interactor, tag in self._wheel_observers:
            interactor.RemoveObserver(tag)
        self._wheel_observers.clear()
        self._wheel_event_tags.clear()

    def on_frame_wheel(self, interactor, event):
        if not self._active or not self.session.case:
            return
        effect = self.editor.activeEffect()
        if effect and effect.name == "SmartContour":
            return
        if interactor.GetControlKey() or interactor.GetShiftKey() or interactor.GetAltKey():
            return
        self.go_to_frame(self._frame + (1 if event == "MouseWheelForwardEvent" else -1))
        # Prevent native scrolling from applying a second, orientation-based step.
        interactor.GetCommand(self._wheel_event_tags[event]).SetAbortFlag(1)

    def go_to_frame(self, frame):
        if self._updating or not self.session.case or not hasattr(self, "slice_widget"):
            return
        frame = max(0, min(int(frame), self.session.shape[0]-1))
        self._updating = True
        try:
            matrix = vtk.vtkMatrix4x4()
            self.session.volume.GetIJKToRASMatrix(matrix)
            point = matrix.MultiplyPoint((0, 0, frame, 1))
            node = self.slice_widget.mrmlSliceNode()
            normal = [node.GetSliceToRAS().GetElement(i, 2) for i in range(3)]
            offset = sum(point[i]*normal[i] for i in range(3))
            self.slice_widget.sliceLogic().SetSliceOffset(offset)
            self._frame = frame
            self.frame_spin.setValue(frame)
            self.frame_slider.setValue(frame)
        finally:
            self._updating = False
        self.update_frame_text()

    def on_slice_changed(self, caller=None, event=None):
        if self._updating or not self.session.case or not self._active:
            return
        node = self.slice_widget.mrmlSliceNode()
        slice_matrix = node.GetSliceToRAS()
        # Keep editing in the original frame plane even if another module rotates it.
        if any(abs(slice_matrix.GetElement(i, j)-self._plane[i][j]) > 1e-6 for i in range(3) for j in range(3)):
            self._updating = True
            for i in range(3):
                for j in range(3):
                    slice_matrix.SetElement(i, j, self._plane[i][j])
            node.UpdateMatrices()
            self._updating = False
        inverse = vtk.vtkMatrix4x4()
        self.session.volume.GetRASToIJKMatrix(inverse)
        ijk = inverse.MultiplyPoint(tuple(slice_matrix.GetElement(i, 3) for i in range(3)) + (1,))
        self.go_to_frame(round(ijk[2]))

    def update_frame_text(self):
        frame = self._frame
        matches = [f"肿瘤{number} · {r.category} {r.start}–{r.end}"
                   for number, r in enumerate(self.session.case.ranges, 1) if r.contains(frame)]
        locked = frame in self.session.doctor_frames
        text = f"帧索引 {frame} · 第 {frame+1}/{self.session.shape[0]} 帧\n" + (" / ".join(matches) or "表格范围外")
        text += "\n医生参考帧 · 可重新标注" if locked else "\n可编辑"
        for number, item in enumerate(self.session.case.ranges, 1):
            if item.contains(frame):
                references = [str(int(k)) for k in self.session.doctor_frames if item.contains(int(k))]
                text += f"\n肿瘤{number} 医生参考帧：" + (", ".join(references) or "未找到")
        self.frame_label.text = text
        annotation = self.slice_widget.sliceView().cornerAnnotation()
        annotation.SetText(vtk.vtkCornerAnnotation.UpperLeft, self.session.case.uid + "\n" + text)
        range_text = "标注范围（每行一个肿瘤，帧索引）\n" + (
            "\n".join(("▶ " if item.contains(frame) else "") +
                      f"肿瘤{number} · {item.category}：{item.start}–{item.end}"
                      for number, item in enumerate(self.session.case.ranges, 1))
            or "未解析到标注范围，请核对原始备注")
        annotation.SetText(vtk.vtkCornerAnnotation.UpperRight, range_text)
        self.slice_widget.sliceView().scheduleRender()

    def clear_overlay(self):
        if hasattr(self, "slice_widget"):
            annotation = self.slice_widget.sliceView().cornerAnnotation()
            annotation.SetText(vtk.vtkCornerAnnotation.UpperLeft, "")
            annotation.SetText(vtk.vtkCornerAnnotation.UpperRight, "")
            self.slice_widget.sliceView().scheduleRender()

    def jump_selected(self, index):
        frame = self.jump_combo.itemData(index)
        if frame is not None and int(frame) >= 0:
            self.go_to_frame(int(frame))

    def size_changed(self, effect, value):
        slicer.app.settings().setValue("BreastContour/" + effect + "Pixels", value)
        self.apply_brush()

    def begin_space_smart(self):
        if self._space_smart or not self._active or not self.session.case:
            return
        effect = self.editor.activeEffect()
        self._space_restore_effect = effect.name if effect else ""
        self._space_smart = True
        self.editor.setActiveEffectByName("SmartContour")
        self.on_effect_changed()
        effect = self.editor.activeEffect()
        if effect and effect.name == "SmartContour":
            # Preview at the last image mouse position without requiring movement.
            effect.self().processInteractionEvents(
                self.slice_widget.sliceView().interactor(),
                vtk.vtkCommand.MouseMoveEvent, self.slice_widget)
        else:
            self.end_space_smart()

    def end_space_smart(self):
        if not self._space_smart:
            return
        restore = self._space_restore_effect
        self._space_smart = False
        self._space_restore_effect = ""
        effect = self.editor.activeEffect()
        if effect and effect.name == "SmartContour":
            effect.self().clear()
        self.editor.setActiveEffectByName(restore)
        self.on_effect_changed()

    def on_effect_changed(self):
        effect = self.editor.activeEffect()
        self._last_effect = effect.name if effect else None
        if effect and effect.name == "SmartContour":
            implementation = effect.self()
            implementation.message_callback = self.smart_message
            implementation.threshold_callback = self.sync_smart_tolerance
            implementation.set_tolerance(self.smart_tolerance.value)
            implementation.set_reference_radius(self.smart_radius.value)
        self.apply_brush()
        self.hide_native_options()

    def smart_message(self, message):
        self.status.text = message

    def sync_smart_tolerance(self, value):
        old = self.smart_tolerance.blockSignals(True)
        self.smart_tolerance.setValue(value)
        self.smart_tolerance.blockSignals(old)

    def smart_tolerance_changed(self, value):
        slicer.app.settings().setValue("BreastContour/SmartTolerance", value)
        effect = self.editor.activeEffect() if hasattr(self, "editor") else None
        if effect and effect.name == "SmartContour":
            effect.self().set_tolerance(value)

    def smart_radius_changed(self, value):
        slicer.app.settings().setValue("BreastContour/SmartRadius", value)
        effect = self.editor.activeEffect() if hasattr(self, "editor") else None
        if effect and effect.name == "SmartContour":
            effect.self().set_reference_radius(value)

    def hide_native_options(self):
        # Native options include shared size and sphere controls; our two pixel
        # controls replace them. Paint/Erase buttons and segment selection stay.
        for child in self.editor.findChildren("QWidget"):
            if str(child.objectName) in ("OptionsGroupBox", "EffectsOptionsFrame"):
                child.hide()

    def brush_parameters_changed(self, caller=None, event=None):
        if self._brush_busy or not self.session.volume:
            return
        effect = self.editor.activeEffect()
        if (effect.name if effect else None) != self._last_effect:
            self.on_effect_changed()
        elif effect and effect.name in ("Paint", "Erase"):
            # Our two persistent pixel controls own diameter; native shared
            # parameters must never overwrite the other tool's preference.
            self.apply_brush()
            self.hide_native_options()

    def apply_brush(self):
        if self._brush_busy or not self.session.volume:
            return
        effect = self.editor.activeEffect()
        if not effect or effect.name not in ("Paint", "Erase"):
            return
        self._brush_busy = True
        try:
            pixels = self.paint_size.value if effect.name == "Paint" else self.erase_size.value
            effect.setCommonParameter("BrushDiameterIsRelative", 0)
            effect.setCommonParameter("BrushSphere", 0)
            effect.setCommonParameter("EditIn3DViews", 0)
            effect.setCommonParameter("BrushPixelMode", 0)
            effect.setCommonParameter("BrushAbsoluteDiameter", pixels * self.session.volume.GetSpacing()[0])
            effect.setParameter("ColorSmudge", 0)
            if effect.name == "Erase":
                effect.setParameter("EraseAllSegments", 0)
        finally:
            self._brush_busy = False

    def set_doctor_visible(self, visible):
        if self.session.doctor:
            self.session.doctor.GetDisplayNode().SetVisibility(bool(visible))

    def on_data_changed(self, repaired):
        self.save_button.enabled = True
        self.status.text = "当前标注未保存；已有检查结果需重新检查。"

    def run_check(self):
        if not self.session.case:
            return None
        effect = self.editor.activeEffect()
        restore_effect = effect.name if effect else "Paint"
        self.editor.setActiveEffectByName("")
        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        self.status.text = "正在检查轮廓…"
        slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)
        try:
            self.issues = self.session.check()
            self.issue_list.clear()
            for issue in self.issues:
                suffix = ""
                self.issue_list.addItem(f"{issue.frame} · {issue.segment or '所有分段'} · {issue.kind}{suffix}")
                self.issue_list.item(self.issue_list.count-1).setToolTip(issue.detail)
            self.status.text = f"检查完成：{len(self.issues)} 条待复核提示。检查不会修改轮廓。"
            return self.issues
        except Exception as exc:
            self.error(exc)
            return None
        finally:
            slicer.app.restoreOverrideCursor()
            self.editor.setActiveEffectByName(restore_effect)
            self.on_effect_changed()

    def issue_selected(self, index):
        if 0 <= index < len(self.issues):
            issue = self.issues[index]
            if issue.segment:
                self.editor.setCurrentSegmentID(named_segments(self.session.work)[issue.segment])
            self.go_to_frame(issue.frame)

    def save(self):
        if not self.session.case or self._loading:
            return False
        issues = self.run_check()
        if issues is None:
            return False
        if issues:
            box = qt.QMessageBox(slicer.util.mainWindow())
            box.setWindowTitle("保存前复核")
            box.setText(f"发现 {len(issues)} 条提示，允许保存尚未完成的标注。")
            proceed = box.addButton("仍然保存", qt.QMessageBox.AcceptRole)
            back = box.addButton("返回修正", qt.QMessageBox.RejectRole)
            box.setDefaultButton(back)
            box.exec()
            if box.clickedButton() != proceed:
                return False
        effect = self.editor.activeEffect()
        restore_effect = effect.name if effect else "Paint"
        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        self.editor.setActiveEffectByName("")
        try:
            backup = self.session.save()
            self.status.text = "已保存：" + str(self.session.case.output) + ("\n旧结果备份：" + backup.name if backup else "")
            return True
        except Exception as exc:
            self.error(exc)
            return False
        finally:
            slicer.app.restoreOverrideCursor()
            self.editor.setActiveEffectByName(restore_effect)
            self.on_effect_changed()

    def on_scene_close(self, caller=None, event=None):
        self.detach()
        self.session.close()
        self.current_index = -1
        self.annotation_panel.enabled = False
        self.frame_label.text = "场景已关闭，请重新选择病例"


class SaveKeyFilter(qt.QObject):
    def __init__(self, widget):
        super().__init__(slicer.app)
        self.widget = widget

    def eventFilter(self, obj, event):
        kind = event.type()
        widget = self.widget
        # Release may arrive after focus moved or a dialog/module opened.
        if widget._space_smart:
            if kind in (qt.QEvent.ApplicationDeactivate, qt.QEvent.WindowDeactivate):
                widget.end_space_smart()
            elif kind == qt.QEvent.Show and obj.inherits("QDialog"):
                widget.end_space_smart()
            elif kind == qt.QEvent.KeyRelease and event.key() == qt.Qt.Key_Space:
                if not event.isAutoRepeat():
                    widget.end_space_smart()
                event.accept()
                return True
        if not self.widget._active or not self.widget.session.case or slicer.app.activeModalWidget():
            return False
        if kind in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress) and event.key() == qt.Qt.Key_Space:
            if widget._space_smart:
                event.accept()
                return True
            if event.modifiers() != qt.Qt.NoModifier:
                return False
            # Preserve ordinary spaces and button activation in the Qt panel.
            target = obj
            in_image = False
            editing_text = False
            while target is not None:
                if any(target.inherits(name) for name in
                       ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit")):
                    editing_text = True
                if target == getattr(widget, "slice_widget", None):
                    in_image = True
                    break
                target = target.parent()
            focus = slicer.app.focusWidget()
            while focus is not None:
                if any(focus.inherits(name) for name in
                       ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit")):
                    editing_text = True
                    break
                focus = focus.parent()
            if editing_text:
                return False
            if not in_image and not editing_text and hasattr(widget, "slice_widget"):
                view = widget.slice_widget.sliceView()
                in_image = view.isVisible() and view.rect.contains(view.mapFromGlobal(qt.QCursor.pos()))
            if not in_image:
                return False
            event.accept()
            if kind == qt.QEvent.KeyPress and not event.isAutoRepeat():
                widget.begin_space_smart()
            return True
        if event.type() in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress):
            if event.key() in (qt.Qt.Key_S, qt.Qt.Key_Z) and event.modifiers() == qt.Qt.ControlModifier:
                event.accept()
                if event.type() == qt.QEvent.KeyPress and not event.isAutoRepeat():
                    if event.key() == qt.Qt.Key_S:
                        self.widget.save()
                    else:
                        self.widget.editor.undo()
                return True
        return False
