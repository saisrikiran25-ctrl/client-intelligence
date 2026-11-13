"""Real document ingestion: PDF, scanned PDF, image and text files -> plain text for the agents.

Text-layer PDFs are parsed locally (pypdf). Scanned PDFs and photos have no text layer, so they are read by Gemini's vision
(OCR). OCR output is flagged so the pipeline always sends such documents to a human: a misread digit cannot be detected by
checks that run on the OCR text itself.
"""
from __future__ import annotations

import io

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

OCR_PROMPT = (
    "Transcribe ALL the text in this document exactly as printed, in reading order, one printed line per output line. "
    "Preserve every number, identifier, symbol and date exactly. Do NOT correct, translate, summarise or infer anything. "
    "If a character is unreadable write [?]. Output the plain text only, with no commentary."
)


def pdf_to_text(data: bytes) -> str:
    """Extract the text layer of a PDF. Scanned (image-only) PDFs have none and raise ValueError."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if len(text) < 40:
        raise ValueError("this PDF has no extractable text (probably a scan)")
    return text


def ocr(data: bytes, mime: str, llm=None) -> str:
    """Read a scan or photo with Gemini vision. Raises if the model returns nothing usable."""
    from core.llm import LLM

    llm = llm or LLM("gemini", cache=False)
    text = llm.complete(OCR_PROMPT, attachments=[(mime, data)]).strip()
    if len(text) < 30:
        raise ValueError("OCR returned too little text; the scan may be unreadable")
    return text


def read_document(name: str, data: bytes, llm=None) -> tuple[str, bool]:
    """Return (text, used_ocr)."""
    n = name.lower()
    ext = n[n.rfind("."):] if "." in n else ""
    if ext == ".pdf":
        try:
            return pdf_to_text(data), False
        except ValueError:
            return ocr(data, "application/pdf", llm), True
    if ext in IMAGE_MIME:
        return ocr(data, IMAGE_MIME[ext], llm), True
    return data.decode("utf-8", errors="replace"), False


def file_to_text(name: str, data: bytes) -> str:  # kept for callers that only need the text
    return read_document(name, data)[0]
