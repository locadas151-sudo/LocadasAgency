"""Text aus PDF-Anhängen ziehen."""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

MAX_PAGES = 6


def pdf_to_text(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                parts = []
                for page in pdf.pages[:MAX_PAGES]:
                    parts.append(page.extract_text() or "")
                text = "\n".join(parts).strip()
                if text:
                    return text
        except Exception as exc:
            log.warning("pdfplumber fehlgeschlagen: %s", exc)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:MAX_PAGES]).strip()
    except Exception as exc:
        log.warning("pypdf fehlgeschlagen: %s", exc)
        return ""
