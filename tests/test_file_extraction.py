"""Round-trip fidelity tests for submitted-file extraction.

Every fixture is a genuine file built with the same libraries the agent uses,
so a passing test means the text a rubric judge reads really does carry the
content.
"""

from __future__ import annotations

import io
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from docx import Document
from pptx import Presentation
from pptx.util import Inches as PptxInches

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apexagents import (  # noqa: E402
    ApexAgents,
    _docx_to_text,
    _pptx_to_text,
    _recalculate_xlsx,
    _xlsx_to_text,
)

HAS_LIBREOFFICE = bool(shutil.which("soffice") or shutil.which("libreoffice"))


def extract(files: dict[str, bytes]) -> tuple[str, list[dict]]:
    """Run the environment's extraction over a set of submitted files."""
    holder = SimpleNamespace()
    text = ApexAgents._extract_text_from_files(holder, files)
    return text, holder.extraction_diagnostics


# --- docx --------------------------------------------------------------------


def build_docx() -> bytes:
    doc = Document()
    doc.add_paragraph("Executive summary paragraph.")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Revenue"
    table.cell(0, 1).text = "1,234.5"
    table.cell(1, 0).text = "EBITDA margin"
    table.cell(1, 1).text = "18.7%"

    section = doc.sections[0]
    section.header.paragraphs[0].text = "CONFIDENTIAL DRAFT"
    section.footer.paragraphs[0].text = "Page footer marker 99"

    para = doc.add_paragraph("Footnote host.")
    para.add_run(" trailing")

    textbox_host = doc.add_paragraph()
    run = textbox_host.add_run()
    _add_textbox(run, "Sidebar callout 42")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _add_textbox(run, text: str) -> None:
    """Attach a real w:txbxContent drawing to a run."""
    from docx.oxml.ns import nsmap, qn
    from docx.oxml import parse_xml

    xml = (
        '<w:pict xmlns:w="{w}" xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:shape style="width:200pt;height:50pt">'
        "<v:textbox><w:txbxContent><w:p><w:r><w:t>{t}</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict>"
    ).format(w=nsmap["w"], t=text)
    run._r.append(parse_xml(xml))
    assert run._r.find(qn("w:pict")) is not None


def test_docx_body_table_header_and_footer_survive():
    text = _docx_to_text(build_docx())
    assert "Executive summary paragraph." in text
    # The table is the deliverable in a financial doc; its numbers must survive.
    assert "1,234.5" in text
    assert "18.7%" in text
    assert "Revenue\t1,234.5" in text
    assert "CONFIDENTIAL DRAFT" in text
    assert "Page footer marker 99" in text
    assert "Sidebar callout 42" in text


def test_docx_footnotes_survive():
    """Word keeps footnotes in their own package part, outside the body."""
    footnotes_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/>'
        "</w:r></w:p></w:footnote>"
        '<w:footnote w:id="1"><w:p><w:r>'
        "<w:t>Source: audited FY24 accounts, note 12.</w:t>"
        "</w:r></w:p></w:footnote>"
        "</w:footnotes>"
    ).encode()

    original = build_docx()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            dst.writestr("word/footnotes.xml", footnotes_xml)

    text = _docx_to_text(buf.getvalue())
    assert "=== Footnotes ===" in text
    assert "Source: audited FY24 accounts, note 12." in text
    # The separator footnote Word always writes is not content.
    assert text.count("=== Footnotes ===") == 1


def test_docx_uppercase_extension_is_extracted():
    text, diags = extract({"deliverable.DOCX": build_docx()})
    assert "Executive summary paragraph." in text
    assert diags == [{"file": "deliverable.DOCX", "status": "ok", "format": ".docx"}]


# --- xlsx --------------------------------------------------------------------


def build_workbook_with_formula() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Units"
    ws["B1"] = 12
    ws["A2"] = "Price"
    ws["B2"] = 25
    ws["A3"] = "Revenue"
    ws["B3"] = "=B1*B2"
    ws["A4"] = "Net adjustment"
    ws["B4"] = 0  # a genuine zero, not a blank
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def strip_value_cache(content: bytes) -> bytes:
    """Round-trip through openpyxl the way the environment's Excel tools do.

    openpyxl discards the cached results Excel wrote, which is exactly the
    state a workbook is in after the agent edits it.
    """
    wb = openpyxl.load_workbook(io.BytesIO(content))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_openpyxl_round_trip_really_drops_the_value_cache():
    """The premise of the recalculation path."""
    content = strip_value_cache(build_workbook_with_formula())
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    assert wb["Model"]["B3"].value is None


def test_zero_is_not_erased():
    text, _ = _xlsx_to_text(build_workbook_with_formula())
    row = [line for line in text.splitlines() if line.startswith("Net adjustment")]
    assert row == ["Net adjustment | 0"], text


def test_formula_source_is_reported_alongside_values():
    text, _ = _xlsx_to_text(build_workbook_with_formula())
    assert "=== Formulas ===" in text
    assert "B3: =B1*B2" in text


@pytest.mark.skipif(not HAS_LIBREOFFICE, reason="LibreOffice not installed")
def test_uncached_formula_is_recalculated():
    content = strip_value_cache(build_workbook_with_formula())
    text, status = _xlsx_to_text(content)
    assert status == "libreoffice"
    # 12 * 25 = 300 must reach the judge as a number, not as "=B1*B2".
    assert "Revenue | 300" in text
    assert "B3: =B1*B2" in text


@pytest.mark.skipif(not HAS_LIBREOFFICE, reason="LibreOffice not installed")
def test_cached_workbook_skips_recalculation():
    """A workbook that already carries saved results is read as-is."""
    # LibreOffice writes the results it computes, which is what a workbook
    # saved by Excel looks like.
    cached = _recalculate_xlsx(build_workbook_with_formula())
    assert openpyxl.load_workbook(io.BytesIO(cached), data_only=True)["Model"][
        "B3"
    ].value == 300

    text, status = _xlsx_to_text(cached)
    assert status == "cached"
    assert "Revenue | 300" in text


def test_missing_libreoffice_emits_a_visible_note(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    content = strip_value_cache(build_workbook_with_formula())
    text, status = _xlsx_to_text(content)
    assert status == "unavailable"
    assert "formula results could not be computed" in text
    assert _recalculate_xlsx(content) is None


# --- pptx --------------------------------------------------------------------


def build_pptx() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Valuation summary"

    table_shape = slide.shapes.add_table(
        2, 2, PptxInches(1), PptxInches(2), PptxInches(4), PptxInches(1)
    )
    table = table_shape.table
    table.cell(0, 0).text = "IRR"
    table.cell(0, 1).text = "23.4%"
    table.cell(1, 0).text = "MOIC"
    table.cell(1, 1).text = "2.8x"

    left = slide.shapes.add_textbox(
        PptxInches(1), PptxInches(4), PptxInches(2), PptxInches(1)
    )
    left.text_frame.text = "Grouped left note"
    right = slide.shapes.add_textbox(
        PptxInches(4), PptxInches(4), PptxInches(2), PptxInches(1)
    )
    right.text_frame.text = "Grouped right note"
    slide.shapes.add_group_shape([left, right])

    slide.notes_slide.notes_text_frame.text = "Speaker note: leverage at 4.5x."

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pptx_table_group_and_notes_survive():
    text = _pptx_to_text(build_pptx())
    assert "Valuation summary" in text
    assert "IRR\t23.4%" in text
    assert "MOIC\t2.8x" in text
    assert "Grouped left note" in text
    assert "Grouped right note" in text
    assert "Speaker note: leverage at 4.5x." in text


# --- dispatch and failure visibility -----------------------------------------


def test_unsupported_extension_is_visible_not_empty():
    text, diags = extract({"model.xls": b"\xd0\xcf\x11\xe0legacy"})
    assert "EXTRACTION FAILED" in text
    assert "unsupported file type .xls" in text
    assert diags == [{"file": "model.xls", "status": "unsupported", "format": ".xls"}]


def test_extensionless_file_is_visible_not_empty():
    text, diags = extract({"workspace/output": b"data"})
    assert "unsupported file type (no extension)" in text
    assert diags[0]["status"] == "unsupported"


def test_corrupt_file_reports_the_failure():
    text, diags = extract({"broken.docx": b"not a zip"})
    assert "EXTRACTION FAILED" in text
    assert diags[0]["status"] == "error"
    assert diags[0]["error"]


def test_pdf_is_extracted():
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, "Deal value 4,200")
    pdf.save()

    text, diags = extract({"memo.pdf": buf.getvalue()})
    assert "Deal value 4,200" in text
    assert diags[0]["status"] == "ok"


def test_every_submitted_file_yields_a_block():
    files = {
        "a.docx": build_docx(),
        "b.xlsx": build_workbook_with_formula(),
        "c.bin": b"\x00\x01",
        "d.txt": b"plain text answer",
    }
    text, diags = extract(files)
    for name in files:
        assert f"=== {name} ===" in text
    assert [d["file"] for d in diags] == list(files)
    assert "plain text answer" in text
