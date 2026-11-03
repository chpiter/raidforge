"""Build the per-server DOCX erase report.

One report is written per boot session, after the operator leaves the TUI and
before :meth:`EvidenceSession.finalize` hashes the evidence tree.  It accumulates
every erase run of that boot (including failed, skipped, cancelled and dry-run
attempts) plus the confirmation/progress screenshots captured along the way.

python-docx and Pillow are imported lazily: a missing wheel must degrade to a
``report_failed`` event, never block evidence upload or the exit menu.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import DiskInfo, EraseRun, SnapshotRecord, SubmitResult, SubmitStatus
from .svg_raster import svg_file_to_png

log = logging.getLogger(__name__)

MAX_SCREENSHOTS = 40
MAX_DETAIL_CHARS = 160
_GENERATED_TOKEN = "__RAIDFORGE_REPORT_GENERATED_AT__"

# Official-document typography.  DOCX stores font names only; the appliance
# does not need these fonts installed to generate the report.
LATIN_FONT = "Times New Roman"
TITLE_FONT = "方正小标宋简体"
H1_FONT = "黑体"
H2_FONT = "楷体_GB2312"
H3_FONT = "仿宋_GB2312"
BODY_FONT = "仿宋_GB2312"
TITLE_PT = 22.0       # 二号
BODY_PT = 16.0        # 三号
TABLE_PT = 10.5       # 五号
CAPTION_PT = 10.5
FOOTER_PT = 9.0       # 小五
LINE_PT = 28.0        # fixed line spacing
BODY_INDENT_CHARS = 200  # first-line indent, 2 characters
TABLE_STYLE = "Report Table Text"
CAPTION_STYLE = "Report Caption"
TITLE_STYLE = "Report Title"
SUBTITLE_STYLE = "Report Subtitle"

STATUS_LABELS = {
    SubmitStatus.SUBMITTED.value: "已提交（控制器已接受）",
    SubmitStatus.ASYNC_SUBMITTED.value: "已提交（控制器已接受）",
    SubmitStatus.FAILED.value: "失败",
    SubmitStatus.TIMEOUT.value: "超时",
    SubmitStatus.SKIPPED.value: "跳过（未提交）",
    SubmitStatus.CANCELLED.value: "已取消",
    "CANCELLED_UNSUBMITTED": "取消（未提交）",
    "NOT_ATTEMPTED": "未执行",
}

STEM_LABELS = {
    "confirm": "确认界面",
    "progress": "提交结果",
    "dashboard": "总览",
    "disk-list": "硬盘列表",
    "settings": "设置",
}


def report_filename(server_sn: str, session_started_at: str) -> str:
    """Return ``<serial>-raidforge-report-<YYYYMMDD>.docx`` (UTC date)."""

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", server_sn or "")
    safe = re.sub(r"_+", "_", safe).strip("._-")[:64].strip("._-") or "UNKNOWN"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", session_started_at or "")
    if match:
        date = "".join(match.groups())
    else:  # pragma: no cover - caller always passes an ISO timestamp
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{safe}-raidforge-report-{date}.docx"


def discard_stale_report_files(out_dir: Path) -> List[Path]:
    """Remove temp DOCX files left by a publisher killed mid-build."""

    removed: List[Path] = []
    for path in Path(out_dir).glob(".raidforge-report-*.docx"):
        try:
            path.unlink()
        except OSError:  # pragma: no cover - already gone / read-only
            continue
        removed.append(path)
    return removed


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _stamp(iso: str) -> str:
    """Render an ISO timestamp compactly for the run heading."""

    text = str(iso or "").strip()
    return text.replace("T", " ") if text else "-"


def _disk_type(disk: DiskInfo) -> str:
    """Combine medium and protocol into one operator-facing type label."""

    medium = disk.medium.value if disk.medium.value != "UNKNOWN" else ""
    protocol = disk.protocol.value if disk.protocol.value != "UNKNOWN" else ""
    if medium and protocol:
        return f"{medium} / {protocol}"
    return medium or protocol or "未知"


def _status_text(run: EraseRun, result: Optional[SubmitResult]) -> str:
    if result is None:
        return "取消（未提交）" if run.cancelled else "未执行"
    return STATUS_LABELS.get(result.status.value, result.status.value)


def _detail_text(result: Optional[SubmitResult]) -> str:
    """Last non-empty stdout and stderr lines, both preserved."""

    if result is None:
        return "-"
    parts: List[str] = []
    for text in (result.stdout, result.stderr):
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if lines and lines[-1] not in parts:
            parts.append(lines[-1])
    if not parts:
        return "-"
    return " / ".join(parts)[:MAX_DETAIL_CHARS]


def _unique_disks(
    runs: Sequence[EraseRun], inventory: Sequence[DiskInfo] = ()
) -> List[DiskInfo]:
    """Full boot inventory first, then any run-only disk not in it."""

    seen: set[str] = set()
    disks: List[DiskInfo] = []
    for disk in inventory:
        if disk.disk_id in seen:
            continue
        seen.add(disk.disk_id)
        disks.append(disk)
    for run in runs:
        for plan in run.plans:
            disk = run.disks.get(plan.disk_id)
            if disk is None or disk.disk_id in seen:
                continue
            seen.add(disk.disk_id)
            disks.append(disk)
    return disks


def _erased_marker(
    disk_id: str, selected_ids: set[str], successful_ids: set[str]
) -> str:
    if disk_id not in selected_ids:
        return "未选中"
    if disk_id in successful_ids:
        return "已选中"
    return "已选中（未成功）"


def _snapshots_for_run(run: EraseRun, snapshots: Sequence[SnapshotRecord]) -> List[SnapshotRecord]:
    """Prefer the confirm/progress screenshots, else any screenshot of the run."""

    tagged = [item for item in snapshots if item.run_epoch == run.run_epoch]
    selected: List[SnapshotRecord] = []
    for stem in ("confirm", "progress"):
        for item in tagged:
            if item.stem == stem:
                selected.append(item)
                break
    return selected or tagged


# -- docx helpers ------------------------------------------------------------
def _rpr(element):
    return element.get_or_add_rPr()


def _set_sz_cs(rpr, half_points: int) -> None:
    """Keep complex-script size in sync with w:sz."""

    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for old in rpr.findall(qn("w:szCs")):
        rpr.remove(old)
    sz_cs = OxmlElement("w:szCs")
    sz_cs.set(qn("w:val"), str(half_points))
    sz = rpr.find(qn("w:sz"))
    if sz is not None:
        sz.addnext(sz_cs)
    else:
        rpr.append(sz_cs)


def _set_rfonts(element, east_asia: str, latin: str = LATIN_FONT) -> None:
    """Write explicit CJK/Latin faces and strip theme overrides.

    The default template's heading styles carry ``*Theme`` attributes, which
    Word prefers over explicit fonts; leaving them in place silently ignores
    every font set here.
    """

    from docx.oxml.ns import qn

    rfonts = _rpr(element).get_or_add_rFonts()
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rfonts.attrib.pop(qn(attr), None)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)
    rfonts.set(qn("w:cs"), latin)
    rfonts.set(qn("w:hint"), "eastAsia")


def _blacken(style_or_run) -> None:
    from docx.oxml.ns import qn
    from docx.shared import RGBColor

    style_or_run.font.color.rgb = RGBColor(0, 0, 0)
    try:
        colour = _rpr(style_or_run.element if hasattr(style_or_run, "element") else style_or_run._element).find(
            qn("w:color")
        )
        if colour is not None:
            for attr in ("w:themeColor", "w:themeShade", "w:themeTint"):
                colour.attrib.pop(qn(attr), None)
    except Exception:  # pragma: no cover - python-docx internals changed
        log.debug("cannot clear theme colour", exc_info=True)


def _strip_numbering(element) -> None:
    from docx.oxml.ns import qn

    ppr = element.find(qn("w:pPr"))
    if ppr is None:
        return
    num_pr = ppr.find(qn("w:numPr"))
    if num_pr is not None:
        ppr.remove(num_pr)


def _set_run_font(
    run,
    name: str,
    size=None,
    bold: Optional[bool] = None,
    latin: str = LATIN_FONT,
    color_black: bool = False,
) -> None:
    """Apply split CJK/Latin fonts plus explicit size/bold."""

    from docx.shared import Pt

    run.font.name = latin
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    try:
        element = run._element
        _set_rfonts(element, name, latin)
        if size is not None:
            _set_sz_cs(_rpr(element), int(round(size * 2)))
        if color_black:
            _blacken(run)
    except Exception:  # pragma: no cover - python-docx internals changed
        log.debug("cannot set run font", exc_info=True)


def _style_font(
    style,
    name: str,
    size=None,
    bold: Optional[bool] = None,
    latin: str = LATIN_FONT,
) -> None:
    """Apply split fonts, black colour, size and no auto-numbering to a style."""

    from docx.oxml.ns import qn
    from docx.shared import Pt

    style.font.name = latin
    if size is not None:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    try:
        element = style.element
        _set_rfonts(element, name, latin)
        if size is not None:
            _set_sz_cs(_rpr(element), int(round(size * 2)))
        bcs = _rpr(element).find(qn("w:bCs"))
        if bcs is not None and bold is False:
            bcs.set(qn("w:val"), "0")
        _blacken(style)
        _strip_numbering(element)
    except Exception:  # pragma: no cover
        log.debug("cannot set style font %s", getattr(style, "name", "?"), exc_info=True)


def _no_indent(paragraph) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    pf = paragraph.paragraph_format
    pf.first_line_indent = Pt(0)
    try:
        ind = paragraph._p.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLine"), "0")
        ind.set(qn("w:firstLineChars"), "0")
    except Exception:  # pragma: no cover
        log.debug("cannot reset indent", exc_info=True)


def _set_indent_chars(paragraph, chars: int = BODY_INDENT_CHARS) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    pf = paragraph.paragraph_format
    pf.first_line_indent = Pt(chars * BODY_PT / 100.0)
    try:
        ind = paragraph._p.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLine"), str(int(round(chars * BODY_PT / 100.0 * 20))))
        ind.set(qn("w:firstLineChars"), str(chars))
    except Exception:  # pragma: no cover
        log.debug("cannot set character indent", exc_info=True)


def _exact_lines(paragraph, pt: float = LINE_PT) -> None:
    from docx.shared import Pt

    paragraph.paragraph_format.line_spacing = Pt(pt)


def _single_lines(paragraph) -> None:
    paragraph.paragraph_format.line_spacing = 1.0


def _tight(paragraph, before: float = 0.0, after: float = 0.0) -> None:
    from docx.shared import Pt

    pf = paragraph.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)


def _body(paragraph) -> None:
    _exact_lines(paragraph)
    _set_indent_chars(paragraph)
    _tight(paragraph)


def _add_body(doc, text: str = ""):
    paragraph = doc.add_paragraph(text)
    _body(paragraph)
    return paragraph


def _add_heading(doc, text: str, level: int):
    paragraph = doc.add_heading(text, level=level)
    _no_indent(paragraph)
    _exact_lines(paragraph)
    _tight(paragraph, before=12 if level == 1 else 6, after=6 if level == 1 else 0)
    paragraph.paragraph_format.keep_with_next = True
    return paragraph


def _add_title(doc, text: str):
    paragraph = doc.add_paragraph(text, style=TITLE_STYLE)
    paragraph.alignment = 1  # center
    _no_indent(paragraph)
    _single_lines(paragraph)
    _tight(paragraph, before=0, after=12)
    for run in paragraph.runs:
        _set_run_font(run, TITLE_FONT, TITLE_PT, bold=False, color_black=True)
    return paragraph


def _add_subtitle(doc, text: str):
    paragraph = doc.add_paragraph(text, style=SUBTITLE_STYLE)
    paragraph.alignment = 1
    _no_indent(paragraph)
    _exact_lines(paragraph)
    _tight(paragraph, before=0, after=12)
    for run in paragraph.runs:
        _set_run_font(run, BODY_FONT, BODY_PT, bold=False, color_black=True)
    return paragraph


def _cell_paragraph(cell, text: str, bold: bool = False, header: bool = False):
    paragraph = cell.paragraphs[0]
    paragraph.style = TABLE_STYLE
    for existing in list(paragraph.runs):
        paragraph._p.remove(existing._element)
    _single_lines(paragraph)
    _no_indent(paragraph)
    _tight(paragraph)
    run = paragraph.add_run(str(text) if text not in (None, "") else "-")
    _set_run_font(
        run,
        H1_FONT if header else BODY_FONT,
        TABLE_PT,
        bold=bool(header or bold),
        color_black=True,
    )
    return paragraph


def _style_table(table, widths: Sequence[float], header_repeat: bool = False) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    table.autofit = False
    for index, width in enumerate(widths):
        table.columns[index].width = Cm(width)
    for row in table.rows:
        for index, width in enumerate(widths):
            row.cells[index].width = Cm(width)

    for row in table.rows:
        tr_pr = row._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:cantSplit")) is None:
            tr_pr.append(OxmlElement("w:cantSplit"))
    if header_repeat and table.rows:
        tr_pr = table.rows[0]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            tr_pr.append(OxmlElement("w:tblHeader"))


def _add_kv_table(doc, rows: Sequence[Tuple[str, str]]):
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in rows:
        cells = table.add_row().cells
        _cell_paragraph(cells[0], label, bold=True)
        _cell_paragraph(cells[1], value)
    _style_table(table, (4.2, 12.8))
    return table


def _add_grid(doc, headers: Sequence[str], rows: Sequence[Sequence[Any]], widths=None):
    from docx.enum.table import WD_TABLE_ALIGNMENT

    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    try:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
    except Exception:  # pragma: no cover
        pass
    for index, label in enumerate(headers):
        _cell_paragraph(table.rows[0].cells[index], label, header=True)
    for values in rows:
        cells = table.add_row().cells
        for index, value in enumerate(values):
            _cell_paragraph(cells[index], value)
    _style_table(table, widths or [17.0 / max(1, len(headers))] * len(headers), header_repeat=True)
    return table


def _add_picture(doc, out_dir: Path, record: SnapshotRecord, caption: str) -> bool:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    caption_paragraph = doc.add_paragraph(caption, style=CAPTION_STYLE)
    caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _single_lines(caption_paragraph)
    _no_indent(caption_paragraph)
    _tight(caption_paragraph, after=3)
    for run in caption_paragraph.runs:
        _set_run_font(run, BODY_FONT, CAPTION_PT, color_black=True)

    path = Path(record.path)
    if not path.is_absolute():
        path = out_dir / path
    png = svg_file_to_png(path)
    if not png:
        _add_body(doc, f"截图 {record.name}.svg 无法渲染，原始文件见 {record.path}。")
        return False
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _single_lines(paragraph)
    _no_indent(paragraph)
    _tight(paragraph)
    paragraph.add_run().add_picture(io.BytesIO(png), width=Cm(16))
    return True


def _replace_generated_token(document, value: str) -> None:
    """Swap the timestamp placeholder after all slow rasterisation is done."""

    def _paragraph(paragraph) -> None:
        for run in paragraph.runs:
            if _GENERATED_TOKEN in run.text:
                run.text = run.text.replace(_GENERATED_TOKEN, value)

    for paragraph in document.paragraphs:
        _paragraph(paragraph)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    _paragraph(paragraph)
    for section in document.sections:
        for paragraph in section.footer.paragraphs:
            _paragraph(paragraph)


def _style_no_indent(style) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    pf = style.paragraph_format
    pf.first_line_indent = Pt(0)
    try:
        ind = style.element.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLine"), "0")
        ind.set(qn("w:firstLineChars"), "0")
    except Exception:  # pragma: no cover
        log.debug("cannot reset style indent", exc_info=True)


def _define_styles(document) -> None:
    """Define Normal/headings and the report-specific paragraph styles."""

    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    normal = document.styles["Normal"]
    _style_font(normal, BODY_FONT, BODY_PT, bold=False)
    normal.paragraph_format.line_spacing = Pt(LINE_PT)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    _style_no_indent(normal)

    for name, font, before, after in (
        ("Heading 1", H1_FONT, 12, 6),
        ("Heading 2", H2_FONT, 6, 0),
        ("Heading 3", H3_FONT, 6, 0),
    ):
        style = document.styles[name]
        _style_font(style, font, BODY_PT, bold=False)
        pf = style.paragraph_format
        pf.line_spacing = Pt(LINE_PT)
        pf.space_before = Pt(before)
        pf.space_after = Pt(after)
        pf.keep_with_next = True
        _style_no_indent(style)

    def _custom(name: str, font: str, size: float, *, center: bool, after: float = 0) -> None:
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        _style_font(style, font, size, bold=False)
        pf = style.paragraph_format
        pf.line_spacing = 1.0 if size >= TITLE_PT or size <= TABLE_PT else Pt(LINE_PT)
        pf.space_before = Pt(0)
        pf.space_after = Pt(after)
        if center:
            pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _style_no_indent(style)

    _custom(TITLE_STYLE, TITLE_FONT, TITLE_PT, center=True, after=12)
    _custom(SUBTITLE_STYLE, BODY_FONT, BODY_PT, center=True, after=12)
    _custom(TABLE_STYLE, BODY_FONT, TABLE_PT, center=False)
    _custom(CAPTION_STYLE, BODY_FONT, CAPTION_PT, center=True, after=3)

    footer = document.styles["Footer"]
    _style_font(footer, BODY_FONT, FOOTER_PT, bold=False)
    footer.paragraph_format.line_spacing = 1.0
    footer.paragraph_format.space_before = Pt(0)
    footer.paragraph_format.space_after = Pt(0)
    footer.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_no_indent(footer)


def _write_docx(
    path: Path,
    *,
    server_sn: str,
    session_id: str,
    session_started_at: str,
    out_dir: Path,
    runs: Sequence[EraseRun],
    snapshots: Sequence[SnapshotRecord],
    inventory: Sequence[DiskInfo],
    app_version: str,
    tool: str,
) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)

    _define_styles(document)

    _add_title(document, "硬盘数据擦除报告")
    _add_subtitle(document, f"服务器序列号：{server_sn or 'UNKNOWN'}")

    _add_kv_table(
        document,
        [
            ("服务器序列号", server_sn or "UNKNOWN"),
            ("会话 ID", session_id),
            ("检测工具", tool or "none"),
            ("生成时间", _GENERATED_TOKEN),
        ],
    )

    # -- part 1: server + disk information ----------------------------------
    _add_heading(document, "一、服务器信息", level=1)
    info = _add_body(document)
    run = info.add_run("服务器序列号：")
    _set_run_font(run, BODY_FONT, BODY_PT, bold=True, color_black=True)
    run = info.add_run(server_sn or "UNKNOWN")
    _set_run_font(run, BODY_FONT, BODY_PT, color_black=True)
    _add_body(document, f"会话开始时间：{session_started_at}")

    _add_heading(document, "硬盘信息", level=2)
    disks = _unique_disks(runs, inventory)
    selected_ids = {plan.disk_id for erase_run in runs for plan in erase_run.plans}
    successful_ids = {
        result.disk_id
        for erase_run in runs
        for result in erase_run.results
        if result.ok
    }
    if disks:
        _add_body(
            document,
            f"本次启动共发现 {len(disks)} 块磁盘，其中 {len(selected_ids)} 块被选中擦除。",
        )
        rows = []
        for index, disk in enumerate(disks, start=1):
            rows.append(
                [
                    index,
                    disk.disk_id,
                    disk.size_human,
                    _disk_type(disk),
                    disk.model or "-",
                    disk.serial or "未知",
                    disk.location,
                    disk.firmware_or_raid,
                    _erased_marker(disk.disk_id, selected_ids, successful_ids),
                ]
            )
        _add_grid(
            document,
            [
                "序号",
                "磁盘 ID",
                "容量",
                "类型",
                "型号",
                "序列号",
                "设备/位置",
                "固件/RAID",
                "本次擦除",
            ],
            rows,
            widths=[1.2, 2.1, 1.5, 1.8, 2.1, 2.0, 2.2, 2.2, 1.9],
        )
        _add_body(
            document,
            "数据来源：本次启动的磁盘枚举，与 events.jsonl 的 inventory 事件及 "
            "controller_raw/ 原始输出一致。",
        )
    else:
        _add_body(document, "本次启动未记录到硬盘信息。")

    # -- part 2: process records + screenshots ------------------------------
    _add_heading(document, "二、擦除过程", level=1)

    used_screenshots = 0
    any_picture = False
    for position, erase_run in enumerate(runs):
        heading = _add_heading(document, f"擦除记录 {_stamp(erase_run.started_at)}", level=2)
        if position:
            heading.paragraph_format.page_break_before = True
        _add_body(document, f"开始：{erase_run.started_at}    结束：{erase_run.finished_at}")
        counts = erase_run.status_counts()
        summary = (
            "计划 {planned} 块；已提交 {submitted} 块；失败 {failed} 块；超时 {timeout} 块；"
            "跳过 {skipped} 块；未提交 {unsubmitted} 块。".format(
                planned=counts["planned"],
                submitted=counts["submitted"],
                failed=counts["failed"],
                timeout=counts["timeout"],
                skipped=counts["skipped"],
                unsubmitted=counts["cancelled"] + counts["not_attempted"],
            )
        )
        if counts["unmatched"]:
            summary += f" 另有 {counts['unmatched']} 条提交结果无法匹配到计划，未计入统计。"
        _add_body(document, summary)

        rows = []
        for plan, result in erase_run.attempts():
            rows.append(
                [
                    plan.disk_id,
                    plan.label,
                    plan.dry_run_text,
                    _status_text(erase_run, result),
                    "-" if result is None or result.return_code is None else result.return_code,
                    "-" if result is None else f"{result.duration_sec:.2f}",
                    _detail_text(result),
                ]
            )
        for result in erase_run.unmatched_results():
            rows.append(
                [
                    result.disk_id,
                    "（无对应计划）",
                    "-",
                    _status_text(erase_run, result),
                    result.return_code if result.return_code is not None else "-",
                    f"{result.duration_sec:.2f}",
                    _detail_text(result),
                ]
            )
        _add_grid(
            document,
            ["磁盘 ID", "方法", "命令", "状态", "返回码", "耗时(秒)", "详情"],
            rows,
            widths=[2.1, 1.8, 4.6, 2.2, 1.5, 2.2, 2.6],
        )

        _add_heading(document, "过程截图", level=3)
        images = _snapshots_for_run(erase_run, snapshots)
        if images:
            for record in images:
                if used_screenshots >= MAX_SCREENSHOTS:
                    break
                label = STEM_LABELS.get(record.stem, record.stem)
                if _add_picture(document, out_dir, record, f"{label}（{record.name}.svg）"):
                    any_picture = True
                used_screenshots += 1
        else:
            _add_body(document, "本次擦除未捕获到过程截图。")

    if not any_picture:
        _add_heading(document, "会话截图（未关联到具体擦除记录）", level=2)
        if snapshots:
            for record in snapshots[:MAX_SCREENSHOTS]:
                label = STEM_LABELS.get(record.stem, record.stem)
                _add_picture(document, out_dir, record, f"{label}（{record.name}.svg）")
        else:
            _add_body(document, "本次会话未捕获到截图。")

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _single_lines(footer)
    _no_indent(footer)
    _tight(footer)
    run = footer.add_run(
        f"raidforge {app_version} · 生成于 {_GENERATED_TOKEN} · "
        "原始证据 events.jsonl / session.cast / snapshots 与本报告同目录并已一并上传"
    )
    _set_run_font(run, BODY_FONT, FOOTER_PT, color_black=True)

    _replace_generated_token(document, _now_utc())
    document.save(str(path))


def build_erase_report(
    *,
    server_sn: str,
    session_id: str,
    session_started_at: str,
    out_dir: Path,
    runs: Sequence[EraseRun],
    snapshots: Sequence[SnapshotRecord],
    inventory: Sequence[DiskInfo] = (),
    app_version: str,
    tool: str,
) -> Optional[Path]:
    """Write the one DOCX report for this boot.

    Returns:
        Path of the report, or ``None`` when no erase run happened.

    Raises:
        Exception from python-docx/Pillow; :func:`finalize_session` records a
        ``report_failed`` event and continues.
    """

    if not runs:
        return None
    out_dir = Path(out_dir)
    target = out_dir / report_filename(server_sn, session_started_at)
    handle, temp_name = tempfile.mkstemp(dir=str(out_dir), prefix=".raidforge-report-", suffix=".docx")
    os.close(handle)
    try:
        _write_docx(
            Path(temp_name),
            server_sn=server_sn,
            session_id=session_id,
            session_started_at=session_started_at,
            out_dir=out_dir,
            runs=runs,
            snapshots=snapshots,
            inventory=inventory,
            app_version=app_version,
            tool=tool,
        )
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
        return target
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:  # pragma: no cover
            pass
        raise
