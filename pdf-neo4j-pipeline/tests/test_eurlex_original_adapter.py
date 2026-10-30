from pathlib import Path

import fitz

from normtext_extractor.eurlex_original import extract_pdf, extract_xhtml


def record(media_type: str, local_path: str):
    return {
        "base_celex": "31968R0804",
        "consolidated_celex": "31968R0804",
        "descriptor": "R",
        "descriptor_label": "regulation",
        "title": "Verordnung (EWG) Nr. 804/68",
        "local_path": local_path,
        "media_type": media_type,
        "download_url": "https://publications.europa.eu/resource/celex/31968R0804",
    }


def test_pdf_fallback_emits_addressable_articles(tmp_path: Path) -> None:
    path = tmp_path / "31968R0804.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "VERORDNUNG (EWG) Nr. 804/68\nPräambel\nArtikel 1\nDiese Verordnung gilt.\nArtikel 2\nSie tritt in Kraft.",
    )
    document.save(path)
    document.close()

    payload = extract_pdf(path, record("application/pdf", path.name))
    extracted = payload["documents"][0]
    articles = [
        unit for unit in extracted["structural_units"] if unit["unit_type"] == "article"
    ]

    assert extracted["metadata"]["source_format"] == "eurlex_pdf_original"
    assert extracted["document_global_key"] == "celex_31968r0804"
    assert [unit["global_key"] for unit in articles] == [
        "celex_31968r0804_art_1",
        "celex_31968r0804_art_2",
    ]
    assert all(unit["page_range"] == {"start": 1, "end": 1} for unit in articles)
    assert any(
        issue["issue_type"] == "eurlex_pdf_fallback"
        for issue in payload["extraction_issues"]
    )


def test_xhtml_fallback_emits_addressable_articles(tmp_path: Path) -> None:
    path = tmp_path / "31968R0804.xhtml"
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <div class="eli-main-title"><p class="oj-doc-ti">VERORDNUNG (EWG) Nr. 804/68</p></div>
  <div class="eli-subdivision" id="pbl_1"><p>Präambel</p></div>
  <div class="eli-subdivision" id="art_1">
    <p class="oj-ti-art">Artikel 1</p><p class="oj-normal">Diese Verordnung gilt.</p>
  </div>
</body></html>
""",
        encoding="utf-8",
    )

    payload = extract_xhtml(path, record("application/xhtml+xml", path.name))
    extracted = payload["documents"][0]
    article = next(
        unit for unit in extracted["structural_units"] if unit["unit_type"] == "article"
    )

    assert extracted["metadata"]["source_format"] == "eurlex_xhtml_original"
    assert article["global_key"] == "celex_31968r0804_art_1"
    assert "Diese Verordnung gilt" in article["text"]


def test_legacy_html_directive_emits_addressable_articles(tmp_path: Path) -> None:
    path = tmp_path / "32001L0019.html"
    path.write_text(
        """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html lang="DE"><body><div id="TexteOnly">
<p>Richtlinie 2001/19/EG</p><p>Präambel</p>
<p>Artikel 1</p><p>Diese Richtlinie gilt.</p>
<p>Artikel 2</p><p>Sie tritt in Kraft.</p>
</div></body></html>""",
        encoding="utf-8",
    )
    directive_record = {
        **record("text/html", path.name),
        "base_celex": "32001L0019",
        "consolidated_celex": "32001L0019",
        "descriptor": "L",
        "descriptor_label": "directive",
        "legal_value": "DIR",
        "title": "Richtlinie 2001/19/EG",
    }

    payload = extract_xhtml(path, directive_record)
    extracted = payload["documents"][0]
    articles = [
        unit for unit in extracted["structural_units"] if unit["unit_type"] == "article"
    ]

    assert extracted["metadata"]["source_format"] == "eurlex_html_original"
    assert extracted["metadata"]["legal_value"] == "DIR"
    assert [unit["global_key"] for unit in articles] == [
        "celex_32001l0019_art_1",
        "celex_32001l0019_art_2",
    ]
    assert any(
        issue["issue_type"] == "eurlex_legacy_html_fallback"
        for issue in payload["extraction_issues"]
    )
