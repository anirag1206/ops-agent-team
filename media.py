"""Pictures and files from WhatsApp and email, made ready for the CEO AI (Grok).

Nothing here uses AI. Pictures are passed to Grok as they are (Grok can look at them).
Files are turned into plain text on your PC for free: .txt .csv .docx .xlsx, and .pdf
when the small free package pypdf is installed (pip install pypdf).
"""
import base64
import io
import json
import re
import urllib.request
import zipfile

import whatsmcp_bridge as wa

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")


def fetch_whatsapp(account_id, message_id):
    """Download a WhatsApp picture or file through WhatsMCP. Returns (bytes, mime) or (None, '')."""
    r = wa._call("GET", f"/accounts/{account_id}/messages/{message_id}/media")
    if r.get("status") != "ok":
        print("Could not download attachment:", (r.get("refusal") or {}).get("message") or r.get("status"))
        return None, ""
    if r.get("media_base64"):
        return base64.b64decode(r["media_base64"]), r.get("mime", "")
    if r.get("media_url"):
        with urllib.request.urlopen(r["media_url"], timeout=120) as f:
            return f.read(), r.get("mime", "")
    return None, ""


def file_text(data, mime="", name="", limit=2500):
    """Free text from a file, or '' if it cannot be read on this PC."""
    name, mime = (name or "").lower(), (mime or "").lower()
    try:
        if name.endswith((".txt", ".csv")) or mime.startswith("text/"):
            text = data.decode("utf-8", "replace")
        elif name.endswith(".docx") or "wordprocessingml" in mime:
            xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
        elif name.endswith(".xlsx") or "spreadsheetml" in mime:
            z = zipfile.ZipFile(io.BytesIO(data))
            words = re.findall(r"<t[^>]*>([^<]*)</t>", z.read("xl/sharedStrings.xml").decode("utf-8", "replace")) \
                if "xl/sharedStrings.xml" in z.namelist() else []
            text = " | ".join(words)
        elif name.endswith(".pdf") or mime == "application/pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:10])
        else:
            return ""
    except Exception as e:
        print("Could not read file", name, e)
        return ""
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:limit] + (" ...(cut)" if len(text) > limit else "")


def as_image(data, mime):
    """(mime, base64) for a picture Grok can look at, or None."""
    mime = (mime or "").split(";")[0].lower()
    if data and mime in IMAGE_TYPES and len(data) <= 5 * 1024 * 1024:
        return mime, base64.b64encode(data).decode()
    return None
