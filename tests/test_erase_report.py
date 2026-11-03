"""Per-server DOCX report: filename, content, manifest and wrapper inheritance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("docx", reason="needs python-docx to build the report")
pytest.importorskip("PIL", reason="needs Pillow to embed screenshots")

from docx import Document  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core import erase_report  # noqa: E402
from raidforge.core.erase_report import build_erase_report, report_filename  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import (  # noqa: E402
    CommandPlan,
    DetectionResult,
    DiskInfo,
    EraseRun,
    Medium,
    Protocol,
    SnapshotRecord,
    SubmitResult,
    SubmitStatus,
    Vendor,
)
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.main import finalize_session  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402

SESSION_START = "2026-11-02T03:04:05Z"
SESSION_DATE = "20261102"

SIMPLE_SVG = """<svg viewBox="0 0 40 20" xmlns="http://www.w3.org/2000/svg">
  <style>.m { fill: #c5c8c6; font-size: 10px }</style>
  <rect x="0" y="0" width="40" height="20" fill="#000000"/>
  <g transform="translate(2, 5)">
    <text class="m" x="0" y="0" textLength="24">CONFIRM</text>
  </g>
</svg>
"""


def _disk(
    disk_id: str,
    medium: Medium,
    serial: str,
    model: str = "ST2000NM0008",
    firmware: str = "",
) -> DiskInfo:
    return DiskInfo(
        disk_id=disk_id,
        controller_index=0,
        vendor=Vendor.LSI,
        medium=medium,
        protocol=Protocol.SAS,
        enclosure=32,
        slot=0,
        model=model,
        serial=serial,
        firmware=firmware,
        size_bytes=2000398934016,
        raid_state="Onln",
    )


def _plan(disk: DiskInfo) -> CommandPlan:
    return CommandPlan(
        disk_id=disk.disk_id,
        kind="erase",
        label="storcli /c0/e32/s0 start erase",
        argv=["storcli", "/c0/e32/s0", "start", "erase"],
        medium=disk.medium,
        vendor=Vendor.LSI,
    )


def _result(disk_id: str, status: SubmitStatus, rc=0, err: str = "") -> SubmitResult:
    return SubmitResult(
        disk_id=disk_id,
        kind="erase",
        status=status,
        return_code=rc,
        duration_sec=1.25,
        stderr=err,
    )


def _run(
    index: int,
    epoch: int,
    disks,
    results,
    *,
    cancelled: bool = False,
    dry_run: bool = False,
) -> EraseRun:
    return EraseRun(
        index=index,
        run_epoch=epoch,
        started_at=SESSION_START,
        finished_at="2026-11-02T03:05:00Z",
        dry_run=dry_run,
        cancelled=cancelled,
        plans=[_plan(disk) for disk in disks],
        results=list(results),
        disks={disk.disk_id: disk for disk in disks},
    )


def _snapshot(out_dir: Path, name: str, stem: str, epoch: int) -> SnapshotRecord:
    relative = f"snapshots/{name}.svg"
    target = out_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SIMPLE_SVG, encoding="utf-8")
    return SnapshotRecord(name=name, stem=stem, path=relative, run_epoch=epoch)


def _document_text(document) -> str:
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _cfg(tmp_path: Path):
    return load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False, "wait_for_route_seconds": 0},
            "tools": {"command_timeout": 5},
        }
    )


def _context(tmp_path: Path, session_id=None) -> AppContext:
    cfg = _cfg(tmp_path)
    sid = resolve_server_id(force_values={"system_serial": "SNTEST01"})
    ev = EvidenceSession(cfg, sid.serial, session_id=session_id)
    return AppContext(cfg=cfg, evidence=ev, server_id=sid)


# ---------------------------------------------------------------------------
# filename
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "serial,expected_prefix",
    [
        ("SN/1 2", "SN_1_2"),
        ("  SERVER-01  ", "SERVER-01"),
        ("", "UNKNOWN"),
        ("x" * 100, "x" * 64),
    ],
)
def test_report_filename_sanitises_serial(serial, expected_prefix):
    name = report_filename(serial, SESSION_START)
    assert name == f"{expected_prefix}-raidforge-report-{SESSION_DATE}.docx"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def test_no_runs_writes_no_report(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    assert (
        build_erase_report(
            server_sn="SNTEST01",
            session_id="SNTEST01_20261102T030405Z",
            session_started_at=SESSION_START,
            out_dir=out_dir,
            runs=[],
            snapshots=[],
            inventory=[],
            app_version="test",
            tool="none",
        )
        is None
    )
    assert not list(out_dir.glob("*.docx"))


def test_two_runs_produce_one_report_with_all_results(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1", firmware="SN02")
    ssd = _disk("c0e32s1", Medium.SSD, "BTWL1234", model="S4510", firmware="XCV10120")
    untouched = _disk("c0e32s2", Medium.HDD, "UNTOUCH1", model="ST4000NM000A")
    run1 = _run(1, 1, [hdd], [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)])
    run2 = _run(
        2,
        2,
        [hdd, ssd],
        [
            _result(hdd.disk_id, SubmitStatus.FAILED, rc=1, err="controller busy"),
            _result(ssd.disk_id, SubmitStatus.SKIPPED),
        ],
    )
    snapshots = [
        _snapshot(out_dir, "01-confirm", "confirm", 1),
        _snapshot(out_dir, "02-progress", "progress", 1),
        _snapshot(out_dir, "03-confirm", "confirm", 2),
        _snapshot(out_dir, "04-progress", "progress", 2),
    ]

    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run1, run2],
        snapshots=snapshots,
        inventory=[hdd, ssd, untouched],
        app_version="test",
        tool="storcli",
    )
    assert report is not None
    assert report.name == f"SNTEST01-raidforge-report-{SESSION_DATE}.docx"
    assert len(list(out_dir.glob("*-raidforge-report-*.docx"))) == 1

    document = Document(str(report))
    text = _document_text(document)
    assert "硬盘数据擦除报告" in text
    assert "擦除记录" in text
    assert "ZTN1" in text and "BTWL1234" in text
    assert "UNTOUCH1" in text and "未选中" in text
    assert "1.82 TB" in text
    assert "SSD / SAS" in text and "HDD / SAS" in text
    assert "RAID Onln / FW SN02" in text
    assert "RAID Onln / FW XCV10120" in text
    assert "已提交（控制器已接受）" in text
    assert "失败" in text
    assert "跳过（未提交）" in text
    assert "controller busy" in text
    assert "storcli /c0/e32/s0 start erase" in text
    for header in ("设备/位置", "固件/RAID", "本次擦除", "命令"):
        assert header in text
    for removed in (
        "SN 来源",
        "主机名",
        "操作模式",
        "擦除轮次",
        "第 1 次",
        "第 2 次",
        "第 1 轮",
        "第 2 轮",
        "实擦",
        "干跑",
    ):
        assert removed not in text, f"removed field still present: {removed}"
    # Every selected screenshot must have been embedded.
    assert len(document.inline_shapes) == len(snapshots)


def test_cancelled_run_marks_unsubmitted_plans(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1")
    ssd = _disk("c0e32s1", Medium.SSD, "BTWL1234")
    run = _run(
        1,
        1,
        [hdd, ssd],
        [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)],
        cancelled=True,
    )
    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run],
        snapshots=[],
        app_version="test",
        tool="storcli",
    )
    assert report is not None
    text = _document_text(Document(str(report)))
    assert "取消（未提交）" in text
    assert "未提交 1 块" in text
    assert "本次擦除未捕获到过程截图" in text


def test_dry_run_report_does_not_disclose_mode(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1")
    run = _run(
        1,
        1,
        [hdd],
        [_result(hdd.disk_id, SubmitStatus.SKIPPED, rc=None)],
        dry_run=True,
    )
    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run],
        snapshots=[],
        app_version="test",
        tool="none",
    )
    assert report is not None
    text = _document_text(Document(str(report)))
    assert "跳过（未提交）" in text
    for removed in ("干跑", "实擦", "操作模式", "未向控制器提交任何命令", "模拟结果"):
        assert removed not in text


# ---------------------------------------------------------------------------
# finalize_session integration
# ---------------------------------------------------------------------------
def _two_run_context(tmp_path: Path) -> AppContext:
    ctx = _context(tmp_path)
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1", firmware="SN02")
    ssd = _disk("c0e32s1", Medium.SSD, "BTWL1234", model="S4510", firmware="XCV10120")
    untouched = _disk("c0e32s2", Medium.HDD, "UNTOUCH1", model="ST4000NM000A")
    ctx.inventory = [hdd, ssd, untouched]
    ctx.detection = DetectionResult(tool="storcli", vendor=Vendor.LSI, controllers=[])
    ctx.erase_history = [
        _run(1, 1, [hdd], [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)]),
        _run(2, 2, [ssd], [_result(ssd.disk_id, SubmitStatus.SKIPPED)]),
    ]
    ctx.snapshot_log = [
        _snapshot(ctx.evidence.root, "01-confirm", "confirm", 1),
        _snapshot(ctx.evidence.root, "02-progress", "progress", 1),
        _snapshot(ctx.evidence.root, "03-confirm", "confirm", 2),
        _snapshot(ctx.evidence.root, "04-progress", "progress", 2),
    ]
    return ctx


def test_finalize_session_hashes_report_and_parent_inherits_it(tmp_path):
    ctx = _two_run_context(tmp_path)
    manifest = finalize_session(ctx)

    name = manifest["report_file"]
    expected = report_filename("SNTEST01", ctx.evidence.started_at)
    assert name == expected
    assert name.startswith("SNTEST01-raidforge-report-")
    assert name.endswith(".docx")
    report_path = ctx.evidence.root / name
    assert report_path.is_file()
    assert len(manifest["erase_runs"]) == 2
    assert manifest["report_generated_at"]
    assert manifest["tool"] == "storcli"
    assert manifest["disk_count"] == 3
    assert len(manifest["submissions"]) == 2
    assert len(manifest["inventory"]) == 3

    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert name in entries
    assert len(entries[name]["sha256"]) == 64
    report_bytes = report_path.read_bytes()

    # Wrapper mode: the parent process has no in-memory history but re-finalises
    # to hash session.cast.  It must adopt the child's report, not drop it.
    parent = _context(tmp_path, session_id=ctx.evidence.session_id)
    parent_manifest = finalize_session(parent)
    assert parent_manifest["report_file"] == name
    assert len(parent_manifest["erase_runs"]) == 2
    assert parent_manifest["tool"] == "storcli"
    assert parent_manifest["disk_count"] == 3
    assert len(parent_manifest["submissions"]) == 2
    assert len(parent_manifest["inventory"]) == 3
    assert report_path.read_bytes() == report_bytes
    assert len(list(ctx.evidence.root.glob("*-raidforge-report-*.docx"))) == 1
    entries = {entry["path"]: entry for entry in parent_manifest["files"]}
    assert entries[name]["sha256"] == hashlib.sha256(report_bytes).hexdigest()


def test_attempts_pair_by_disk_id_not_position(tmp_path):
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1")
    ssd = _disk("c0e32s1", Medium.SSD, "BTWL1234")
    run = _run(
        1,
        1,
        [hdd, ssd],
        [
            _result(ssd.disk_id, SubmitStatus.FAILED, rc=1, err="ssd failed"),
            _result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED),
        ],
    )
    pairs = {plan.disk_id: result for plan, result in run.attempts()}
    assert pairs[hdd.disk_id].status is SubmitStatus.ASYNC_SUBMITTED
    assert pairs[ssd.disk_id].status is SubmitStatus.FAILED
    counts = run.status_counts()
    assert counts["submitted"] == 1 and counts["failed"] == 1

    out_dir = tmp_path / "session"
    out_dir.mkdir()
    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run],
        snapshots=[],
        inventory=[hdd, ssd],
        app_version="test",
        tool="storcli",
    )
    text = _document_text(Document(str(report)))
    assert "ssd failed" in text


def test_unmatched_result_is_visible_not_borrowed(tmp_path):
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1")
    run = _run(
        1,
        1,
        [hdd],
        [
            _result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED),
            _result("ghost0", SubmitStatus.FAILED, rc=9, err="orphan result"),
        ],
    )
    assert len(run.unmatched_results()) == 1
    assert run.status_counts()["unmatched"] == 1

    out_dir = tmp_path / "session"
    out_dir.mkdir()
    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run],
        snapshots=[],
        inventory=[hdd],
        app_version="test",
        tool="storcli",
    )
    text = _document_text(Document(str(report)))
    assert "（无对应计划）" in text
    assert "ghost0" in text
    assert "orphan result" in text
    assert "另有 1 条提交结果无法匹配到计划" in text


def test_generated_timestamp_placeholder_is_replaced(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1")
    run = _run(1, 1, [hdd], [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)])
    report = build_erase_report(
        server_sn="SNTEST01",
        session_id="SNTEST01_20261102T030405Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run],
        snapshots=[],
        inventory=[hdd],
        app_version="test",
        tool="storcli",
    )
    text = _document_text(Document(str(report)))
    assert "__RAIDFORGE_REPORT_GENERATED_AT__" not in text
    assert "UTC" in text


# ---------------------------------------------------------------------------
# official-document typography
# ---------------------------------------------------------------------------
def _format_report(tmp_path):
    out_dir = tmp_path / "format"
    out_dir.mkdir()
    hdd = _disk("c0e32s0", Medium.HDD, "ZTN1", firmware="SN02")
    run1 = _run(1, 1, [hdd], [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)])
    run2 = _run(2, 2, [hdd], [_result(hdd.disk_id, SubmitStatus.ASYNC_SUBMITTED)])
    snapshots = [
        _snapshot(out_dir, "01-confirm", "confirm", 1),
        _snapshot(out_dir, "02-progress", "progress", 1),
        _snapshot(out_dir, "03-confirm", "confirm", 2),
        _snapshot(out_dir, "04-progress", "progress", 2),
    ]
    report = build_erase_report(
        server_sn="FMT01",
        session_id="FMT01_20261103T010000Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[run1, run2],
        snapshots=snapshots,
        inventory=[hdd],
        app_version="test",
        tool="storcli",
    )
    return Document(str(report))


def _rfonts(element):
    from docx.oxml.ns import qn

    return element.rPr.rFonts


def test_normal_style_uses_fangsong_16_and_exact_28pt():
    import tempfile
    from pathlib import Path

    from docx.oxml.ns import qn
    from docx.shared import Pt

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    style = document.styles["Normal"]
    rfonts = _rfonts(style.element)
    assert rfonts.get(qn("w:eastAsia")) == "仿宋_GB2312"
    assert rfonts.get(qn("w:ascii")) == "Times New Roman"
    assert rfonts.get(qn("w:hAnsi")) == "Times New Roman"
    assert "w:asciiTheme" not in rfonts.attrib
    assert "w:eastAsiaTheme" not in rfonts.attrib
    assert style.font.size == Pt(16)
    assert style.paragraph_format.line_spacing == Pt(28)
    from docx.enum.text import WD_LINE_SPACING

    assert style.paragraph_format.line_spacing_rule == WD_LINE_SPACING.EXACTLY
    assert style.font.color.rgb is not None
    assert str(style.font.color.rgb) == "000000"


def test_heading_styles_have_required_fonts_sizes_and_no_numbering():
    import tempfile
    from pathlib import Path

    from docx.oxml.ns import qn
    from docx.shared import Pt

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    expected = {
        "Heading 1": "黑体",
        "Heading 2": "楷体_GB2312",
        "Heading 3": "仿宋_GB2312",
    }
    for name, font in expected.items():
        style = document.styles[name]
        assert _rfonts(style.element).get(qn("w:eastAsia")) == font
        assert style.font.size == Pt(16)
        assert "w:asciiTheme" not in _rfonts(style.element).attrib
        ppr = style.element.find(qn("w:pPr"))
        assert ppr is None or ppr.find(qn("w:numPr")) is None


def test_title_is_22pt_xiaobiaosong_not_bold_centered():
    import tempfile
    from pathlib import Path

    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    title = next(p for p in document.paragraphs if p.text == "硬盘数据擦除报告")
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    run = title.runs[0]
    assert run.font.size == Pt(22)
    assert run.bold is not True
    rfonts = _rfonts(run._element)
    assert rfonts.get(qn("w:eastAsia")) == "方正小标宋简体"
    assert rfonts.get(qn("w:ascii")) == "Times New Roman"
    assert run._element.rPr.find(qn("w:sz")).get(qn("w:val")) == "44"


def test_body_has_two_character_indent_and_exact_28pt():
    import tempfile
    from pathlib import Path

    from docx.oxml.ns import qn
    from docx.shared import Pt

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    body = next(p for p in document.paragraphs if p.text.startswith("会话开始时间"))
    assert body.paragraph_format.line_spacing == Pt(28)
    assert body.paragraph_format.first_line_indent == Pt(32)
    ind = body._p.pPr.find(qn("w:ind"))
    assert ind.get(qn("w:firstLineChars")) == "200"
    assert ind.get(qn("w:firstLine")) == "640"


def test_tables_are_10_5pt_single_spaced_with_fixed_header():
    import tempfile
    from pathlib import Path

    from docx.oxml.ns import qn
    from docx.shared import Pt

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    disk_table = document.tables[1]
    cell = disk_table.rows[1].cells[0]
    paragraph = cell.paragraphs[0]
    assert paragraph.style.name == "Report Table Text"
    assert paragraph.paragraph_format.line_spacing == 1.0
    assert paragraph.paragraph_format.first_line_indent == Pt(0)
    run = paragraph.runs[0]
    assert run.font.size == Pt(10.5)
    assert _rfonts(run._element).get(qn("w:eastAsia")) == "仿宋_GB2312"
    assert _rfonts(run._element).get(qn("w:ascii")) == "Times New Roman"
    assert disk_table.autofit is False
    tbl_pr = disk_table._tbl.tblPr
    assert tbl_pr.find(qn("w:tblLayout")).get(qn("w:type")) == "fixed"
    header_tr = disk_table.rows[0]._tr
    assert header_tr.find(qn("w:trPr")).find(qn("w:tblHeader")) is not None


def test_second_run_starts_with_page_break_before_not_blank_paragraph():
    import tempfile
    from pathlib import Path

    from docx.oxml.ns import qn

    with tempfile.TemporaryDirectory() as tmp:
        document = _format_report(Path(tmp))
    headings = [p for p in document.paragraphs if p.text.startswith("擦除记录")]
    assert len(headings) == 2
    assert headings[1].paragraph_format.page_break_before is True
    assert 'w:type="page"' not in document.element.xml

    image_paragraphs = [
        p for p in document.paragraphs if p._p.findall(".//" + qn("w:drawing"))
    ]
    assert image_paragraphs
    for paragraph in image_paragraphs:
        assert paragraph.paragraph_format.line_spacing == 1.0


def test_report_failure_leaves_no_temp_file_and_is_logged(tmp_path, monkeypatch):
    ctx = _two_run_context(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("docx writer exploded")

    monkeypatch.setattr(erase_report, "_write_docx", _boom)
    manifest = finalize_session(ctx)

    assert manifest["report_file"] is None
    leftovers = list(ctx.evidence.root.glob("*.docx")) + list(ctx.evidence.root.glob(".raidforge-report-*"))
    assert leftovers == []

    events = [
        json.loads(line)
        for line in ctx.evidence.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failures = [event for event in events if event.get("event") == "report_failed"]
    assert failures, "report_failed was not recorded"
    assert "docx writer exploded" in failures[-1]["error"]
