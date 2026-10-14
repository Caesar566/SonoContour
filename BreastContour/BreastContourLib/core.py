"""File discovery, workbook parsing and pixel contour checks (no Slicer imports)."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
from scipy import ndimage as ndi


@dataclass(frozen=True)
class FrameRange:
    category: str
    start: int
    end: int

    def contains(self, frame):
        return self.start <= frame <= self.end


@dataclass
class Case:
    uid: str
    directory: Path
    note: str = ""
    ranges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    in_workbook: bool = False

    @property
    def annotation_exempt(self):
        note = re.sub(r"\s+", "", self.note)
        return self.in_workbook and (not note or re.fullmatch(r"[1１]类[/／\\]*", note) is not None)

    @property
    def image(self):
        return self.directory / (self.uid + "_ORI.dcm")

    @property
    def doctor(self):
        return self.directory / "Segmentation.nrrd"

    @property
    def output(self):
        return self.directory / "Segment.nrrd"


@dataclass(frozen=True)
class Issue:
    frame: int
    segment: str
    kind: str
    detail: str = ""
    locked: bool = False


def parse_ranges(note, frame_count=None):
    """One range per tumor, in note order, with inherited category."""
    ranges, warnings = [], []
    category = None
    note = re.sub(r"\s+(?=\d+[A-Za-z]*类)", "/", note)
    for part in re.split(r"[/／\\\n]+", note):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(?:(.+?类)\s*)?(\d+)\s*[-–—－~～]\s*(\d+)", part)
        if not match:
            warnings.append("无法解析范围：" + part)
            if "类" in part:
                category = None
            continue
        category = match[1].strip() if match[1] else category
        if category is None:
            warnings.append("范围缺少类别：" + part)
            continue
        a, b = sorted((int(match[2]), int(match[3])))
        ranges.append(FrameRange(category, a, b))
        if frame_count is not None and b >= frame_count:
            warnings.append(f"范围 {a}-{b} 超出帧索引 0-{frame_count - 1}")
    if not ranges:
        warnings.append("没有可用的病灶范围")
    return ranges, warnings


def workbook_notes(path):
    """Read only stage-three rows; supports shared and inline Excel strings."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    notes = {}
    with zipfile.ZipFile(path) as book:
        strings = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = ["".join(t.text or "" for t in si.findall(".//s:t", ns))
                       for si in ET.fromstring(book.read("xl/sharedStrings.xml"))]
        for filename in sorted(book.namelist()):
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", filename):
                continue
            rows = []
            for row in ET.fromstring(book.read(filename)).findall(".//s:row", ns):
                values = {}
                for cell in row.findall("s:c", ns):
                    col = re.match(r"[A-Z]+", cell.attrib["r"])[0]
                    value = cell.find("s:v", ns)
                    text = value.text or "" if value is not None else ""
                    if cell.get("t") == "s":
                        text = strings[int(text)]
                    elif cell.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//s:t", ns))
                    values[col] = text.strip()
                rows.append(values)
            in_stage = False
            note_col = None
            for row in rows:
                stage = next((v for v in row.values() if re.search(r"阶段[一二三四五六七八九十]", v)), None)
                if stage:
                    in_stage = "阶段三" in stage
                    note_col = None
                if not in_stage:
                    continue
                for col, value in row.items():
                    if value == "备注":
                        note_col = col
                if note_col:
                    uid = next((v for v in row.values() if re.fullmatch(r"2\.25\.\d+", v)), None)
                    if uid:
                        if uid in notes:
                            raise ValueError("阶段三有重复病例 ID：" + uid)
                        notes[uid] = row.get(note_col, "")
    if not notes:
        raise ValueError("XLSX 中未找到阶段三的体位编号及备注记录")
    return notes


def discover_cases(root):
    root = Path(root).expanduser().resolve()
    notes = workbook_notes(root / "segmentations.xlsx")
    ids = set(notes) | {p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"2\.25\.\d+", p.name)}
    cases = []
    for uid in sorted(ids):
        case = Case(uid, root / uid, notes.get(uid, ""), in_workbook=uid in notes)
        if not case.annotation_exempt:
            case.ranges, case.warnings = parse_ranges(case.note)
        if uid not in notes:
            case.warnings.append("阶段三表格中缺少该病例")
        for path in (case.image, case.doctor):
            if not path.is_file():
                case.warnings.append("缺少文件：" + path.name)
        cases.append(case)
    return cases


def skeletonize(binary):
    """Zhang-Suen thinning, padded to preserve border behavior; never edits input."""
    image = np.pad(np.asarray(binary, dtype=bool), 1)
    while True:
        changed = False
        for step in (0, 1):
            p = image[1:-1, 1:-1]
            n = [image[:-2, 1:-1], image[:-2, 2:], image[1:-1, 2:], image[2:, 2:],
                 image[2:, 1:-1], image[2:, :-2], image[1:-1, :-2], image[:-2, :-2]]
            count = sum(x.astype(np.uint8) for x in n)
            transitions = sum((~n[i] & n[(i+1) % 8]).astype(np.uint8) for i in range(8))
            if step == 0:
                removable = ~(n[0] & n[2] & n[4]) & ~(n[2] & n[4] & n[6])
            else:
                removable = ~(n[0] & n[2] & n[6]) & ~(n[0] & n[4] & n[6])
            delete = p & (count >= 2) & (count <= 6) & (transitions == 1) & removable
            if np.any(delete):
                p[delete] = False
                changed = True
        if not changed:
            return image[1:-1, 1:-1]


def contour_problems(mask):
    """Return advisory findings for each 8-connected foreground component."""
    mask = np.asarray(mask, dtype=bool)
    labels, count = ndi.label(mask, np.ones((3, 3), dtype=np.uint8))
    findings = []
    for index, slices in enumerate(ndi.find_objects(labels), 1):
        if slices is None:
            continue
        component = labels[slices] == index
        touches = any(s.start == 0 or s.stop == mask.shape[axis] for axis, s in enumerate(slices))
        padded = np.pad(component, 1)
        enclosed = ndi.binary_fill_holes(padded, structure=ndi.generate_binary_structure(2, 1)) & ~padded
        if not enclosed.any():
            findings.append(("疑似未闭合", f"轮廓 {index} 未围出内部区域"))
        skeleton = skeletonize(component)
        degree = ndi.convolve(skeleton.astype(np.uint8), np.ones((3, 3), dtype=np.uint8), mode="constant") - skeleton
        endpoints = int(np.count_nonzero(skeleton & (degree == 1)))
        n = np.pad(skeleton, 1)
        neighbors = [n[:-2, 1:-1], n[:-2, 2:], n[1:-1, 2:], n[2:, 2:],
                     n[2:, 1:-1], n[2:, :-2], n[1:-1, :-2], n[:-2, :-2]]
        crossings = sum((~neighbors[i] & neighbors[(i+1) % 8]).astype(np.uint8) for i in range(8))
        branches = bool(np.any(skeleton & (crossings >= 3)))
        if branches or (enclosed.any() and endpoints):
            findings.append(("存在分支", f"轮廓 {index}：骨架端点 {endpoints}，请复核"))
        if touches:
            findings.append(("接触图像边界", f"轮廓 {index} 接触原始图像边缘"))
    return findings


def check_arrays(named_arrays, ranges, locked_frames=(), progress=None):
    issues = []
    locked = set(locked_frames)
    occupied = None
    for name, array in named_arrays.items():
        nonempty = np.any(array, axis=(1, 2))
        occupied = nonempty.copy() if occupied is None else occupied | nonempty
        for frame in np.flatnonzero(nonempty):
            if progress:
                progress(int(frame), name)
            for kind, detail in contour_problems(array[frame]):
                issues.append(Issue(int(frame), name, kind, detail, frame in locked))
    if occupied is not None:
        expected = {k for r in ranges for k in range(max(0, r.start), min(len(occupied), r.end + 1))}
        for frame in sorted(expected):
            if not occupied[frame]:
                issues.append(Issue(frame, "", "待确认空帧", "表格范围内所有分段均为空", frame in locked))
    return sorted(issues, key=lambda x: (x.frame, x.segment, x.kind))


def atomic_save(target, writer, validator):
    """Write and validate first; backup then replace. Old output survives failures."""
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix=".Segment-", suffix=".seg.nrrd", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary)
    backup = None
    try:
        writer(temporary)
        if not temporary.stat().st_size:
            raise IOError("写入结果为空")
        validator(temporary)
        # Windows disk flushing requires a writable handle. r+b preserves the
        # validated bytes without truncation and also works on POSIX systems.
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = target.with_name(f"Segment.backup-{stamp}.nrrd")
            shutil.copy2(target, backup)
            with backup.open("r+b") as stream:
                os.fsync(stream.fileno())
        os.replace(temporary, target)
        return backup
    finally:
        temporary.unlink(missing_ok=True)
