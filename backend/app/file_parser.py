"""
File and email text extraction utilities.

Supported formats:
  Text/Docs  : .txt, .csv, .log, .md, .html, .htm
  Documents  : .pdf, .docx, .doc (best-effort), .xlsx, .xls, .pptx, .ppt (best-effort)
  Email      : .eml, .msg (Outlook binary format, via extract-msg)
  Images     : .jpg, .jpeg, .png, .gif, .bmp, .webp  (via Bedrock Claude vision)
"""

from __future__ import annotations

import io
import re
import email as email_lib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Max file size: 20 MB (enforced by the route handler; belt-and-suspenders here)
MAX_BYTES = 20 * 1024 * 1024


def extract_text_from_file(filename: str, content: bytes) -> str:
    """
    Extract plain text from an uploaded file.
    Returns the extracted text string, or raises ValueError on unsupported type.
    """
    if len(content) > MAX_BYTES:
        raise ValueError(f"File too large ({len(content)//1024} KB). Maximum 20 MB.")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # ── Plain text formats ─────────────────────────────────────────────────
    if ext in ("txt", "csv", "log", "md"):
        return content.decode("utf-8", errors="replace")

    # ── HTML ───────────────────────────────────────────────────────────────
    if ext in ("html", "htm"):
        return _extract_html(content)

    # ── PDF ────────────────────────────────────────────────────────────────
    if ext == "pdf":
        return _extract_pdf(content)

    # ── Word ──────────────────────────────────────────────────────────────
    if ext == "docx":
        return _extract_docx(content)
    if ext == "doc":
        return _extract_doc(content)

    # ── Excel ─────────────────────────────────────────────────────────────
    if ext in ("xlsx", "xls"):
        return _extract_excel(content, ext)

    # ── PowerPoint ────────────────────────────────────────────────────────
    if ext in ("pptx", "ppt"):
        return _extract_pptx(content, ext)

    # ── Email ─────────────────────────────────────────────────────────────
    if ext == "eml":
        return _extract_eml(content)
    if ext == "msg":
        return _extract_msg(content)

    # ── Images (Vision via Bedrock) ────────────────────────────────────────
    if ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp"):
        return _extract_image(content, ext)

    # ── Fallback: try UTF-8 decode ─────────────────────────────────────────
    try:
        text = content.decode("utf-8", errors="replace")
        if len(text.strip()) > 10:
            return text
    except Exception:
        pass

    raise ValueError(
        f"Unsupported file type: .{ext}. "
        "Supported: PDF, DOCX, DOC, XLSX, XLS, PPTX, PPT, "
        "TXT, CSV, LOG, MD, HTML, EML, MSG, JPG, PNG, GIF, BMP, WEBP"
    )


# ── Extractors ────────────────────────────────────────────────────────────────

def _extract_html(content: bytes) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    # Remove script and style elements
    for tag in soup(["script", "style", "head", "meta", "link"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse excessive blank lines
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _extract_pdf(content: bytes) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    result = "\n\n".join(text_parts)
    if not result.strip():
        raise ValueError("PDF appears to be image-only (no selectable text). "
                         "Try uploading a scanned PDF with OCR text layer, "
                         "or save as a DOCX/TXT file.")
    return result


def _extract_docx(content: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(content))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    # Also extract text from tables
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            if row_text:
                parts.append(row_text)
    return "\n".join(parts)


def _extract_doc(content: bytes) -> str:
    """Old .doc format — best-effort using python-docx (works for some .doc files).
    Falls back to raw text extraction."""
    try:
        return _extract_docx(content)
    except Exception:
        pass
    # Fallback: crude extraction of readable ASCII strings from binary
    try:
        text = content.decode("latin-1", errors="replace")
        # Extract runs of printable characters
        printable = re.findall(r'[ -~\n\r\t]{20,}', text)
        result = "\n".join(printable)
        if len(result.strip()) > 50:
            return result + "\n\n[Note: .doc extraction is approximate. Convert to .docx for better results.]"
    except Exception:
        pass
    raise ValueError(
        "Could not extract text from .doc file. "
        "Please save as .docx (Word 2007+) or .pdf and re-upload."
    )


def _extract_excel(content: bytes, ext: str) -> str:
    import openpyxl
    parts = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            parts.append(f"=== Sheet: {sheet_name} ===")
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                # Skip entirely empty rows
                row_values = [str(v).strip() if v is not None else "" for v in row]
                if any(row_values):
                    parts.append(" | ".join(row_values))
                    row_count += 1
                    if row_count >= 500:  # cap rows to avoid huge text
                        parts.append(f"[... truncated at 500 rows for sheet {sheet_name} ...]")
                        break
        wb.close()
    except Exception as e:
        raise ValueError(f"Could not read Excel file: {e}. "
                         "Supported formats: .xlsx, .xls (Excel 97+)")
    return "\n".join(parts)


def _extract_pptx(content: bytes, ext: str) -> str:
    """Extract text from PowerPoint files."""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(content))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text.strip())
            if slide_texts:
                parts.append(f"--- Slide {i} ---")
                parts.extend(slide_texts)
        return "\n".join(parts)
    except Exception as e:
        if ext == "ppt":
            raise ValueError(
                "Could not extract text from .ppt file (old PowerPoint format). "
                "Please save as .pptx (PowerPoint 2007+) and re-upload."
            )
        raise ValueError(f"Could not read PowerPoint file: {e}")


def _extract_eml(content: bytes) -> str:
    msg = email_lib.message_from_bytes(content)
    parts = []

    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    to = msg.get("To", "")
    date = msg.get("Date", "")
    if subject:
        parts.append(f"Subject: {subject}")
    if sender:
        parts.append(f"From: {sender}")
    if to:
        parts.append(f"To: {to}")
    if date:
        parts.append(f"Date: {date}")
    parts.append("")  # blank line separator

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode("utf-8", errors="replace"))
                    break  # prefer first plain text part
        # If no plain text found, try HTML
        if len(parts) <= 5:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(_extract_html(payload))
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            ct = msg.get_content_type()
            if ct == "text/html":
                parts.append(_extract_html(payload))
            else:
                parts.append(payload.decode("utf-8", errors="replace"))

    return "\n".join(parts)


def _extract_msg(content: bytes) -> str:
    """
    Extract text from an Outlook .msg file (binary OLE2/Compound-File
    format — NOT the same as a plain-text .eml). Uses the `extract-msg`
    library (GPL-3 licensed — see requirements.txt), which parses the
    MAPI properties directly from bytes via extract_msg.openMsg().
    """
    try:
        import extract_msg
    except ImportError as e:
        raise ValueError(
            "Server is missing the 'extract-msg' package needed to read .msg files. "
            "Please save the email as .eml or .pdf and re-upload."
        ) from e

    try:
        msg = extract_msg.openMsg(content)
    except Exception as e:
        raise ValueError(
            f"Could not read .msg file: {e}. "
            "Try re-saving the email as .eml or .pdf from Outlook and re-upload."
        )

    try:
        parts = []
        if msg.subject:
            parts.append(f"Subject: {msg.subject}")
        if msg.sender:
            parts.append(f"From: {msg.sender}")
        to = getattr(msg, "to", None)
        if to:
            parts.append(f"To: {to}")
        date = getattr(msg, "date", None)
        if date:
            parts.append(f"Date: {date}")
        parts.append("")  # blank line separator

        body = msg.body
        if body:
            parts.append(body)
        else:
            html_body = getattr(msg, "htmlBody", None)
            if html_body:
                parts.append(_extract_html(html_body if isinstance(html_body, bytes) else html_body.encode("utf-8", errors="replace")))

        result = "\n".join(str(p) for p in parts)
    finally:
        try:
            msg.close()
        except Exception:
            pass

    if not result.strip():
        raise ValueError("Could not extract any text from the .msg file (it may be empty or unsupported).")
    return result


def _extract_image(content: bytes, ext: str) -> str:
    """
    Use Claude Sonnet 5.5 on Amazon Bedrock (multimodal) to extract text / describe an image.
    Falls back to a descriptive placeholder if Bedrock is unavailable.
    """
    import base64
    import boto3
    import json

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "bmp": "image/bmp", "webp": "image/webp",
    }
    media_type = mime_map.get(ext, "image/jpeg")

    # Validate it's actually an image with Pillow
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(content))
        width, height = img.size
        logger.info(f"Image: {width}x{height} {img.mode}")
    except Exception as e:
        raise ValueError(f"Cannot read image file: {e}")

    # Claude accepts only jpeg / png / gif / webp images; anything else (BMP)
    # is re-encoded to PNG first.
    image_format = "jpeg" if ext in ("jpg", "jpeg") else ext
    if image_format not in ("jpeg", "png", "gif", "webp"):
        try:
            buf = io.BytesIO()
            img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(buf, format="PNG")
            content = buf.getvalue()
            image_format = "png"
        except Exception as e:
            raise ValueError(f"Cannot convert image file: {e}")

    try:
        # Same (cross-account, role-assuming) client every other Bedrock call uses.
        from .ai_engine import _get_client, MODEL_ANALYSIS, claude_converse_params
        client = _get_client()

        response = client.converse(
            modelId=MODEL_ANALYSIS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": image_format,
                            "source": {"bytes": content},
                        }
                    },
                    {
                        "text": (
                            "Extract all visible text from this image. "
                            "If the image is a screenshot of a document, email, or error message, "
                            "transcribe all visible text exactly. "
                            "If it's a diagram or photo, describe the key information shown. "
                            "Focus on content relevant to IT infrastructure issues."
                        )
                    }
                ],
            }],
            **claude_converse_params(1000),
        )
        for block in response["output"]["message"]["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip():
                return block["text"]
        raise ValueError("model returned no text")
    except Exception as e:
        logger.warning(f"Bedrock image extraction failed: {e}")
        raise ValueError(
            f"Could not extract text from image via AI. Error: {e}. "
            "For best results, upload a text-based document (PDF, DOCX, TXT) instead."
        )


# ── AI extraction from raw text ───────────────────────────────────────────────

def ai_extract_job_fields(raw_text: str) -> dict:
    """
    Extract job fields from raw text using keyword analysis.
    """
    text_lower = raw_text.lower()

    # ── Guess category from keywords ─────────────────────────────────────────
    category = "Other"
    category_keywords = {
        "Networking": ["vpn", "vlan", "subnet", "routing", "firewall", "dns", "bgp", "network", "connectivity", "ping", "latency", "bandwidth", "cidr", "nat", "gateway"],
        "Compute": ["ec2", "instance", "cpu", "server", "vm", "virtual machine", "autoscaling", "ami", "compute", "ecs", "fargate", "lambda", "eks", "kubernetes"],
        "Storage": ["s3", "ebs", "efs", "storage", "disk", "volume", "bucket", "backup", "snapshot", "file system", "data loss"],
        "Security": ["iam", "permission", "access denied", "security group", "policy", "mfa", "token", "credential", "key", "certificate", "ssl", "tls", "waf", "guard"],
        "AD/Identity": ["active directory", "ad", "domain", "ldap", "sso", "azure ad", "entra", "user account", "group policy", "gpo", "login", "authentication"],
    }
    for cat, keywords in category_keywords.items():
        if any(kw in text_lower for kw in keywords):
            category = cat
            break

    # ── Guess priority from urgency words ─────────────────────────────────────
    priority = "Medium"
    if any(w in text_lower for w in ["urgent", "asap", "critical", "down", "outage", "production down", "immediately", "emergency"]):
        priority = "ASAP"
    elif any(w in text_lower for w in ["high", "important", "impact", "cannot access", "blocking", "major"]):
        priority = "High"
    elif any(w in text_lower for w in ["low", "minor", "cosmetic", "nice to have", "whenever"]):
        priority = "Low"

    # ── Extract a title (first meaningful line) ────────────────────────────────
    title = ""
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("From:", "To:", "Cc:", "Date:", "Subject:", "---", "===")):
            if stripped.lower().startswith("subject:"):
                title = stripped[len("subject:"):].strip()
                break
            if len(stripped) > 8 and len(stripped) < 200:
                title = stripped
                break

    # ── Clean up the description ───────────────────────────────────────────────
    cleaned = re.sub(r'\n{3,}', '\n\n', raw_text).strip()
    description = cleaned[:3000]
    if len(cleaned) > 3000:
        description += "\n\n[... content truncated to 3000 characters ...]"

    return {
        "title": title[:200] if title else "",
        "description": description,
        "category": category,
        "priority": priority,
        "raw_char_count": len(raw_text),
    }
