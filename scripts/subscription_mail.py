"""Shared export + email layout for a Tableau subscription (used by preview_export.py and t6_send.py).

One place for: PDF export, per-view image export (with the 3200 px width cap), and the email body layout copied from
a real Tableau-sent email (H1, inline cid images, PDF line, subscription line, see docs/preview_notes.md).
No SMTP and no database code here.
"""
import io
import os
import re
import struct
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape

import tableauserverclient as TSC

# Display only (the prod sender, DEFAULT_SENDER_EMAIL in .env). Real sends take it from SEND_FROM.


def default_sender(env=os.environ):
    return (env.get("DEFAULT_SENDER_EMAIL") or "").strip()


MAX_IMAGE_BYTES = 1_000_000
MAX_IMAGE_WIDTH = 3200  # px, same as the widest dashboard images Tableau returned
MIN_READABLE_HEIGHT = 150  # px: an image thinner than this after the cap is a sliver (wide crosstab)
VIZ_WIDTH, VIZ_HEIGHT = 1000, 1000  # fixed render area, caps the pixel width of wide crosstabs
PAGE_TYPES = {k.lower(): v for k, v in vars(TSC.PDFRequestOptions.PageType).items() if not k.startswith("_")}
ORIENT = {k.lower(): v for k, v in vars(TSC.PDFRequestOptions.Orientation).items() if not k.startswith("_")}


def safe(s):
    return re.sub(r"[^\w.-]+", "_", s or "x").strip("_")[:60]


def png_size(data):
    return struct.unpack(">II", data[16:24])


def page_count(data):
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except ImportError:
        return len(re.findall(rb"/Type\s*/Page[^s]", data))


def page_problems(page_size, orientation):
    """Unknown or missing page size / orientation of a PDF subscription (the export would silently use Tableau's default)."""
    out = []
    if (page_size or "").lower() not in PAGE_TYPES:
        out.append("page size %r" % page_size)
    if (orientation or "").lower() not in ORIENT:
        out.append("orientation %r" % orientation)
    return out


def pdf_options(page_size, orientation):
    """PDFRequestOptions from the subscription's own page size and orientation (defaults as in Tableau: letter, portrait).
    maxage=1 asks for a render no older than a minute, so the PDF is not served from the view cache."""
    return TSC.PDFRequestOptions(page_type=PAGE_TYPES.get((page_size or "letter").lower()),
                                 orientation=ORIENT.get((orientation or "portrait").lower()), maxage=1)


def export_pdf(server, target, is_wb, page_size, orientation):
    """PDF bytes of the whole workbook or of one view."""
    opts = pdf_options(page_size, orientation)
    if is_wb:
        server.workbooks.populate_pdf(target, opts)
    else:
        server.views.populate_pdf(target, opts)
    return target.pdf


def export_images(server, views, report=None):
    """One PNG per view. Wide crosstabs ignore vizWidth, so anything wider than MAX_IMAGE_WIDTH is downscaled locally.
    Returns [{name, data, resolution, orig_w, orig_h, w, h, capped, unreadable}]; unreadable = thinner than
    MIN_READABLE_HEIGHT after the cap. `report` is an optional print-like callable for progress lines."""
    out = []
    for v in views:
        data, res = None, None
        for res in (TSC.ImageRequestOptions.Resolution.High, None):  # high only if it stays under ~1 MB
            server.views.populate_image(v, TSC.ImageRequestOptions(
                imageresolution=res, maxage=1, viz_width=VIZ_WIDTH, viz_height=VIZ_HEIGHT))
            data = v.image
            if len(data) <= MAX_IMAGE_BYTES:
                break
        ow, oh = png_size(data)
        capped = ow > MAX_IMAGE_WIDTH
        if capped:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im = im.resize((MAX_IMAGE_WIDTH, max(1, round(oh * MAX_IMAGE_WIDTH / ow))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if report:
                report("  '%s' was %d px wide, downscaled to %d px" % (v.name, ow, MAX_IMAGE_WIDTH))
        w, h = png_size(data)
        out.append({"name": v.name, "data": data, "resolution": res, "orig_w": ow, "orig_h": oh, "w": w, "h": h,
                    "capped": capped, "unreadable": h < MIN_READABLE_HEIGHT})
    return out


def build_message(*, ref_kind, ref_name, subject, sender, to_header, note, sched, pdf_bytes, images,
                  msgid_domain="localhost"):
    """The subscription email. images = [(view name, png bytes)]. Layout copied from a real Tableau-sent email.
    `sched` is accepted for the callers but no longer printed: the mail has no "Subscription schedule" line."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender, sender))
    msg["To"] = to_header
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=msgid_domain)
    cids = [(uuid.uuid4(), name, png) for name, png in images]
    lines = []
    if pdf_bytes:
        lines.append("Your Tableau PDF is attached.")
    lines.append("Here's your subscription to the %s %s." % (ref_kind, ref_name))
    note = note or ""
    msg.set_content("\n\n".join(([ref_name] + ([note] if note else [])) + ["[image: %s]" % n for _, n, _ in cids]
                                + lines) + "\n")
    html = "<html><head><meta http-equiv='Content-Type' content='text/html; charset=utf-8'></head><body>"
    html += "<h1>%s</h1>" % escape(ref_name)
    if note:
        html += "<p>%s</p>" % escape(note).replace("\n", "<br>")
    for u, name, _ in cids:
        html += "<p><img src='cid:tableauImage%s'></p>" % u
    for ln in lines:
        html += "<p>%s</p>" % escape(ln)
    html += "</body></html>"
    msg.add_alternative(html, subtype="html")
    for u, name, png in cids:
        msg.get_payload()[1].add_related(png, "image", "png", cid="<tableauImage%s>" % u,
                                         filename="%s.png" % name, disposition="inline")
    if pdf_bytes:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="%s.pdf" % ref_name)
    return msg
