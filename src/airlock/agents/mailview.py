"""What the reader sees of one email: the mailroom turns attachments into text, as plain code.

Real invoices arrive as PDF attachments. The reader is a model with no tools, so it can't open one; the mailroom
can't be talked into anything, so it does the opening. A plain one-part email passes through unchanged. For a
multipart email the reader gets the headers, every text part, and for each PDF its text and its metadata (title,
subject, keywords and the rest), because attackers hide instructions in both, including white text a person
never sees. Everything stays untrusted: this only changes the shape, and customs still trusts none of it.
"""

from __future__ import annotations

import io
import re
from email import message_from_string, policy
from email.message import EmailMessage

MAX_PDF_PAGES = 10
MAX_PDF_CHARS = 6000
HEADERS = ("From", "To", "Date", "Subject", "Reply-To")
META = ("title", "subject", "author", "keywords", "creator", "producer")


def pdf_text(data: bytes) -> str:
    """The text of a PDF and its metadata, or a line saying why it could not be read."""
    from pypdf import PdfReader

    try:
        pdf = PdfReader(io.BytesIO(data))
        meta = pdf.metadata or {}
        lines = [f"PDF {k}: {getattr(meta, k)}" for k in META if getattr(meta, k, None)]
        pages = [_page_text(p) for p in pdf.pages[:MAX_PDF_PAGES]]
    except Exception as e:  # a broken or hostile file is a fact to report, not a crash
        return f"[the mailroom could not read this PDF: {type(e).__name__}]"
    text = "\n".join(lines + [f"--- page {i} ---\n{t}" for i, t in enumerate(pages, start=1)])
    if len(pdf.pages) > MAX_PDF_PAGES:
        text += f"\n[{len(pdf.pages) - MAX_PDF_PAGES} more pages not read]"
    return text if len(text) <= MAX_PDF_CHARS else text[:MAX_PDF_CHARS] + "\n[PDF TEXT TRUNCATED BY MAILROOM]"


def _page_text(page) -> str:
    """The page as laid out (a label stays on the line with its value), blank lines dropped."""
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        text = page.extract_text() or ""
    lines = (re.sub(r" {4,}", "    ", ln.rstrip()) for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln.strip())


def reader_view(raw: str) -> str:
    msg: EmailMessage = message_from_string(raw, policy=policy.default)  # type: ignore[assignment]
    if not msg.is_multipart():
        return raw
    out = [f"{h}: {msg[h]}" for h in HEADERS if msg[h]]
    out.append("")
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype, name = part.get_content_type(), part.get_filename()
        if name is None and ctype in ("text/plain", "text/html"):
            out.append(part.get_content().strip())
        elif ctype == "application/pdf" or (name or "").lower().endswith(".pdf"):
            out.append(f"[attachment {name or 'unnamed.pdf'}: PDF, text extracted by the mailroom]")
            out.append(pdf_text(part.get_payload(decode=True) or b""))
            out.append(f"[end of attachment {name or 'unnamed.pdf'}]")
        else:
            size = len(part.get_payload(decode=True) or b"")
            out.append(f"[attachment {name or 'unnamed'}: {ctype}, {size} bytes, not opened]")
        out.append("")
    return "\n".join(out).strip() + "\n"
