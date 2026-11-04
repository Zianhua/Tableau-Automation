"""Look-and-feel preview: export what a Tableau subscription would attach and build the email.

NOT the production sender. Read-only against Tableau (PDF/PNG export to a local file is the only
server-side action). The email goes ONLY to a local test SMTP server (localhost, see
scripts/preview_smtp.py) that writes .eml files into --out-dir; a copy is also saved directly.
No SES/SMTP credential is read or used. Reads TABLEAU_* from .env (or environment).

Usage:
  python scripts/preview_export.py --subscription-id <id> [--dry-run]
  python scripts/preview_export.py --workbook-name "Gold & Silver Story" [--view-name "..."]
"""
import argparse
import os
import smtplib
import sys
import time
from pathlib import Path

import tableauserverclient as TSC
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subscription_mail import (default_sender, build_message, export_images, export_pdf,  # noqa: E402
                               page_count, safe)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# The SMTP envelope stays localhost-only, see ALLOWED_HOSTS.
ENVELOPE_TO = "preview@localhost"
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}


def find_subscription(server, a):
    subs = list(TSC.Pager(server.subscriptions))
    if a.subscription_id:
        sub = next((s for s in subs if s.id == a.subscription_id), None)
        if not sub:
            sys.exit("Subscription id not found")
        return sub
    wb = next((w for w in TSC.Pager(server.workbooks) if w.name == a.workbook_name), None)
    if not wb:
        sys.exit("Workbook not found: %s" % a.workbook_name)
    views = {}
    if a.view_name:
        server.workbooks.populate_views(wb)
        views = {v.id: v.name for v in wb.views}
    for s in subs:
        if a.view_name:
            if s.target.type.lower() == "view" and views.get(s.target.id) == a.view_name:
                return s
        elif s.target.type.lower() == "workbook" and s.target.id == wb.id:
            return s
    sys.exit("No subscription on that target; use --subscription-id")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subscription-id")
    ap.add_argument("--workbook-name")
    ap.add_argument("--view-name")
    ap.add_argument("--out-dir", default=str(ROOT / "out"))
    ap.add_argument("--smtp-port", type=int, default=1025)
    ap.add_argument("--smtp-host", default="127.0.0.1")
    ap.add_argument("--sender", default=os.environ.get("PREVIEW_FROM", default_sender()),
                    help="From address shown in the email (display only); env PREVIEW_FROM")
    ap.add_argument("--dry-run", action="store_true", help="list what would be exported, export nothing")
    a = ap.parse_args()
    if not a.subscription_id and not a.workbook_name:
        ap.error("give --subscription-id or --workbook-name")
    if a.smtp_host.lower() not in ALLOWED_HOSTS:
        sys.exit("Refusing to connect to SMTP host %r: only localhost/127.0.0.1 is allowed" % a.smtp_host)
    out = Path(a.out_dir)

    auth = TSC.PersonalAccessTokenAuth(os.environ["TABLEAU_PAT_NAME"], os.environ["TABLEAU_PAT_SECRET"],
                                       os.environ.get("TABLEAU_SITE_ID", ""))
    server = TSC.Server(os.environ["TABLEAU_SERVER_URL"], use_server_version=True)
    with server.auth.sign_in(auth):
        sub = find_subscription(server, a)
        user = server.users.get_by_id(sub.user_id)
        orig_to = user.email or "(no email on user)"
        is_wb = sub.target.type.lower() == "workbook"
        if is_wb:
            target = server.workbooks.get_by_id(sub.target.id)
            label = target.name
        else:
            target = server.views.get_by_id(sub.target.id)
            label = "%s / %s" % (server.workbooks.get_by_id(target.workbook_id).name, target.name)
        want_pdf, want_img = bool(sub.attach_pdf), bool(sub.attach_image)
        method = "server.workbooks.populate_pdf (whole workbook)" if is_wb else "server.views.populate_pdf"
        if is_wb:
            server.workbooks.populate_views(target)
            views = list(target.views)  # all tabs the REST view list returns, one image each
        else:
            views = [target]
        try:
            sched = server.schedules.get_by_id(sub.schedule_id).name
        except Exception:
            sched = None
        print("Subscription:", sub.id)
        print("Target: %s '%s'" % (sub.target.type, label))
        print("Subject: %r  attach_pdf=%s attach_image=%s page=%s orientation=%s suspended=%s schedule=%r" % (
            sub.subject, want_pdf, want_img, sub.page_size_option, sub.page_orientation, sub.suspended, sched))
        print("Would export:", ", ".join(
            (["PDF via " + method] if want_pdf else [])
            + (["%d PNG(s): %s" % (len(views), "; ".join(v.name for v in views))] if want_img else [])))
        if not (want_pdf or want_img):
            print("Note: neither PDF nor image flag set; the email would carry no attachment or image.")
        if a.dry_run:
            print("Dry run: nothing exported, nothing sent.")
            return

        out.mkdir(parents=True, exist_ok=True)
        stem = safe(label)
        pdf_bytes = None
        images = []  # (view name, png bytes)
        if want_pdf:
            pdf_bytes = export_pdf(server, target, is_wb, sub.page_size_option, sub.page_orientation)
            (out / (stem + ".pdf")).write_bytes(pdf_bytes)
            print("PDF: %s  %d bytes, %d pages" % (out / (stem + ".pdf"), len(pdf_bytes), page_count(pdf_bytes)))
        if want_img:
            for im in export_images(server, views, report=print):
                images.append((im["name"], im["data"]))
                (out / ("%s_%s.png" % (stem, safe(im["name"])))).write_bytes(im["data"])
                print("PNG '%s': %d bytes, %d x %d px, resolution=%s" % (
                    im["name"], len(im["data"]), im["w"], im["h"], im["resolution"] or "standard"))

    # Layout copied from a real Tableau-sent email, see docs/preview_notes.md
    ref_kind = "workbook" if is_wb else "view"
    ref_name = target.name
    msg = build_message(ref_kind=ref_kind, ref_name=ref_name, subject=sub.subject or label, sender=a.sender,
                        to_header=ENVELOPE_TO,  # the original recipient is deliberately not used
                        note=sub.message, sched=sched, pdf_bytes=pdf_bytes, images=images)

    direct = out / (stem + "_v2_direct.eml")
    direct.write_bytes(bytes(msg))
    print("EML (saved directly): %s  %d bytes" % (direct, direct.stat().st_size))

    before = set(out.glob("*.eml"))
    try:
        with smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=15) as smtp:
            smtp.send_message(msg, from_addr="preview-sender@localhost", to_addrs=[ENVELOPE_TO])
        time.sleep(1)
        for f in sorted(set(out.glob("*.eml")) - before):
            print("EML (via local SMTP): %s  %d bytes" % (f, f.stat().st_size))
    except OSError as e:
        print("Local SMTP not reachable on %s:%d (%s). Start scripts/preview_smtp.py first." % (
            a.smtp_host, a.smtp_port, e))


if __name__ == "__main__":
    main()
