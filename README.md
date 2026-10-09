# 乳腺超声轮廓标注

为 Linux / 3D Slicer 5.12.4 编写的 Python 模块。保留 Segment Editor 的 Paint / Erase 操作，输出为**轮廓线像素**，不填充肿瘤内部。

## 安装

无需编译，也无需安装 Python 包；使用 Slicer 自带的 NumPy、SciPy、VTK 和 Qt。

1. 打开 Slicer 的 **Edit → Application Settings → Modules**。
2. 在 **Additional module paths** 添加包含 `BreastContour.py` 的目录：
   `/home/caesar/Projects/ohslicer/BreastContour`。
3. 重启 Slicer，在 **Segmentation → 乳腺超声轮廓标注** 中打开模块。

也可仅对本次运行加载模块：

```sh
/opt/3dslicer/Slicer --additional-module-path /home/caesar/Projects/ohslicer/BreastContour
```

项目附带 CMake 配置用于在 Slicer 开发环境打包；日常使用直接添加模块路径即可。

也可在项目目录执行 `./launch.sh`，启动 Slicer 并直接打开本模块，无需先修改设置。默认使用 PATH 中的 `Slicer`；可通过 `BREAST_CONTOUR_SLICER` 指定可执行文件。

## 数据目录

选择含 `segmentations.xlsx` 的根目录，结构如下：

```text
数据根目录/
  segmentations.xlsx
  2.25.病例ID/
    2.25.病例ID_ORI.dcm
    Segmentation.nrrd       # 医生原始标注，只读
    Segment.nrrd            # 继续标注及输出；可以不存在
    Segment.backup-时间戳.nrrd
```

模块从表格**阶段三**中读取病例 ID 和“备注”列，保留完整备注。支持如 `2类 138-226/325-347/4A类 229-247/426-389` 的内容：没有新类别的区间继承前一个类别，倒序范围按 `389–426` 处理，重叠区间同时显示。类别只作提示，不自动改变分段。

**帧号使用数组索引，从 0 开始。** 表格中的 138 对应帧索引 138，也就是物理顺序的第 139 帧。图像上同时显示两种计数，避免混淆。

## 使用

1. 选择根目录并点“导入根目录”。病例列表中的警告和悬停提示列出缺少文件、备注解析等问题。
2. 选择分段，用 Paint 沿肿瘤边缘画线；用 Erase 修正，**Ctrl+Z** 撤销上一步标注（本模块激活时）。画笔初始默认 1.5 像素（已有设置继续保留），橡皮默认 15 像素，分别通过模块的两个直径输入框调整并记忆。支持 0.5–200 像素，步长 0.1；可直接输入 1.5；缩放图像不改变原始像素直径。
3. 使用帧索引输入框、滑条、左右按钮或图像滚轮逐帧查看；跳转框提供范围端点及医生帧。
4. 医生标注单独显示为半透明参考，可用“显示医生原始轮廓”开关隐藏。医生标过的帧也可重新标注；Paint / Erase / 撤销只作用于你的工作轮廓。
5. 点“检查轮廓”，在问题列表中点击一项跳到对应帧和分段。检查后仍可继续编辑。
6. 点“一键保存”或按 **Ctrl+S**。如有提示，可返回修正，也可保存未完成的工作。切换病例时会提示保存、放弃或取消。

快捷保存仅在本模块激活时接管；离开模块后恢复 Slicer 的普通保存行为。请使用本模块的一键保存输出 `Segment.nrrd`。

## 医生参考与独立工作文件

- `Segmentation.nrrd` 只作为医生参考读取，模块保存始终写入 `Segment.nrrd`。
- 如果 `Segment.nrrd` 不存在，自动建立空白工作标注，沿用医生分段名称和颜色；不会复制医生轮廓像素。打开病例时立即创建文件，无需手动创建；创建失败会显示错误。
- 如果已有 `Segment.nrrd`，保留其分段和像素，不合并医生分段、不恢复医生帧、不自动清除历史结果。此前版本已写入的医生像素也不会自动删除，可按需用橡皮重新勾勒。
- 参考层有独立可见性开关，不参与编辑、撤销、轮廓检查或输出。图像标记“医生参考帧 · 可重新标注”。
- 画笔和橡皮直径独立记忆，支持小数。关闭 Slicer 的逐点单像素特殊模式，使用二维圆形画笔；像素栅格上的最终线宽仍取决于笔迹位置和方向。
- 落笔后仅标记未保存，不扫描全部医生帧或恢复像素，不创建全体积保护掩膜。完整轮廓检查只在点击检查或保存时运行。
- 覆盖前验证临时 NRRD 能重读、工作轮廓像素和分段属性一致，再备份旧结果并原子替换。写入或验证失败保留旧结果。
- 备份恢复：关闭当前病例，将对应 `Segment.backup-时间戳.nrrd` 复制为同目录的 `Segment.nrrd`，再重新加载。

## 检查判定与边界

前景采用八连通，背景采用四连通，逐个轮廓检查是否围出内部区域；骨架端点和分叉辅助提示。支持同一帧多个闭环，正常的内部空白不是错误。

“疑似未闭合”“存在分支”“接触图像边界”均需人工复核。粗线、自相交或复杂分支会影响数字拓扑判定，模块不会自动补线、填充或确认医学正确性。检查只针对你的工作轮廓。

表格范围内所有分段都没有像素时，单独列为“待确认空帧”；不会把空帧当作未闭合，也不会根据类别猜测应该编辑哪个分段。

第一版针对当前样例的单通道多帧 DCM，沿原始图像平面编辑；不支持彩色超声、动态空间变换或多文件序列。几何不匹配时拒绝静默重采样。输出保留 Slicer 分段信息；暂不导出训练用填充标签。

图像上的中文标注使用明确指定的字体文件：Windows 自动查找系统和当前用户字体目录中的黑体、微软雅黑、宋体或等线；Linux 查找 Noto CJK 或文泉驿字体，Arch 可安装 `noto-fonts-cjk`。替换模块代码后重启 Slicer。字体不存在时，Qt 面板仍显示完整文字。

## 验证

独立逻辑测试：

```sh
/opt/3dslicer/bin/PythonSlicer tests/test_core.py
```

四个实际病例的集成测试（仅在临时目录写入输出）：

```sh
XDG_CONFIG_HOME=/tmp/breast-contour-config \
XDG_CACHE_HOME=/tmp/breast-contour-cache \
QT_QPA_PLATFORM=offscreen \
/opt/3dslicer/Slicer --testing --no-splash --no-main-window \
  --python-script tests/slicer_integration.py
```

真实界面测试，需要桌面显示服务：

```sh
/opt/3dslicer/Slicer --testing --no-splash \
  --additional-module-path "$PWD/BreastContour" \
  --python-script "$PWD/tests/slicer_ui.py"
```

默认测试数据位置为 `/home/caesar/Projects/ohslicer`；独立和集成测试可通过 `BREAST_CONTOUR_DATA` 指定其他数据根目录。详见 `TEST_RESULTS.md`。

实现参考：[Segment Editor 参数接口](https://slicer.readthedocs.io/en/latest/developer_guide/modules/segmenteditor.html)和 [Slicer 分段 NRRD 格式](https://slicer.readthedocs.io/en/latest/developer_guide/modules/segmentations.html)。

## 多肿瘤备注

每个范围对应一个独立肿瘤，按备注出现顺序编号，不合并重叠、相邻或相同范围。类别可继承；支持 `2类 210-222/281-286 3类 399-420` 中没有斜杠的类别切换以及末尾斜杠。

图像右上角每行显示一个肿瘤，当前帧所在行带箭头；重叠范围同时显示。跳转菜单按肿瘤提供起始、结束和范围内医生参考帧。医生 NRRD 的非空帧是参考帧候选，文件没有明确起始/关键/结束角色时不猜测其中哪帧为关键帧；重叠范围中的参考帧同时列入相关肿瘤。范围外参考帧另列。编号仅用于导航和显示，不自动匹配、创建或重命名工作分段。
