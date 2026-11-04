# PDF and email preview (look-and-feel test)

Prototype only. Not the production sender. Nothing was sent to real recipients, nothing on Tableau was changed. The only server-side action was exporting a PDF and a PNG to local files.

## P2 update (2026-10-22): layout matched to a real Tableau email
Reference: one Tableau-sent email (Nova Global Story, 3 dashboards, PDF + images, data-driven schedule). Only its structure was copied. Recipient names, the forwarding header block, the gateway "external party" banner and the SES tracking pixel are NOT reproduced.

### What changed in `scripts/preview_export.py`
- Body now follows the sample: `<h1>` with the workbook (or view) name; one `<p><img src="cid:tableauImage<uuid>"></p>` per image; then "Your Tableau PDF is attached." (only if a PDF is attached); "Here's your subscription to the workbook|view <name>."; a schedule line.
- Attachments follow the subscription flags exactly (attach_pdf, attach_image, D-023): PDF only = no images; image only = no PDF line; both = images plus PDF (the sample's case). If neither flag is set the script says so and sends body text only.
- Workbook subscription: one PNG per entry in `populate_views` (not per worksheet, see below). View subscription: one PNG of that view. Images are inline parts (`Content-ID: <tableauImage...>`), file name `<view name>.png`.
- PDF attachment is named `<Workbook name>.pdf` as in the sample (view subscriptions: `<view name>.pdf`, guess).
- From is `tableau-reports@example.com` (display only; override with `--sender` or env `PREVIEW_FROM`). The header To is now `preview@localhost`; the original recipient is no longer shown. SMTP envelope is still localhost-only (hard block unchanged), no credential.
- Image request: `maxage=1`, fixed `viz_width=1000, viz_height=1000`, `resolution=high` first and standard if the PNG exceeds 1 MB. Wide crosstab worksheets ignore vizWidth (see below), so any PNG wider than 3200 px is downscaled with Pillow (new dependency, `pip install pillow`).
- New file names: `*_v2_direct.eml` plus new `smtp_*.eml`. Old files untouched. `Gold_Silver_Story.pdf` was overwritten with an identical re-export.

### Dashboards vs worksheets
The REST view list (`/workbooks/{id}/views`, API 3.25 here) returns only name and contentUrl; `sheet_type` is empty, so dashboards cannot be told apart from worksheets. The script therefore renders every view the list returns. For CFD Accounts v2 and CFD OI the list is all dashboards (sample-like). Sales Channel - Daily Active Clients lists 5 views (F & O, FX, Precious Metals, CFD, Equities), which are crosstab worksheets, hence the width problem. Hidden sheets are not listed, so the list may already exclude sheets only used inside dashboards, but that is not verifiable here.

### Runs (UAT, not suspended, all to local SMTP only)
| Subscription | Flags | Images (px) | Notes |
|---|---|---|---|
| Gold & Silver Story, "Monthly Gold Story" | PDF only | none | PDF 254,884 B, 6 pages, attached as `Gold & Silver Story.pdf` |
| Sales Channel - Daily Active Clients | image only | 3200x99, 3200x87, 3200x79, 3200x71 (capped), 2616x768 | 5 images, all under 0.2 MB |
| CFD Accounts v2, "CFD Accounts" (the pilot workbook, 7 dashboards) | PDF + image | 1300x1600, 2200x3200, 2200x3200, 3200x3200 x4 | PDF 182,053 B, 7 pages; every PNG under 1 MB (100-560 KB) |
| CFD OI (3 dashboards) | image only | 1500x1600, 3000x3200, 1600x1600 | 0.6-0.8 MB each |
Verified by parsing each smtp_*.eml: part list, every `cid:` reference resolves to an inline image, no unreferenced images, sizes as above.
Fixed 1000x1000 does not shrink fixed-size dashboards (they keep their authored size, 1x or 2x); it is only a request hint.

### The 32,668 px problem
Before: F & O, FX, Precious Metals, CFD were each 32,668 px wide (only Equities was normal, 2,616 px). `viz_width`/`viz_height` has no effect on these crosstabs, so the cap is applied locally: downscale to 3,200 px wide. Result: 3200x99, 3200x87, 3200x79, 3200x71 px (were 32668 x 1012/892/808/728); Equities unchanged at 2616x768. The capped images are thin slivers (about 80-100 px tall) and effectively unreadable, because the crosstabs are one very wide row of columns; the proper fix is to subscribe to a dashboard rather than the raw crosstab sheets, which is a report-design question.

## Remaining guesses
- Schedule line wording. Sample (data-driven): "You receive this subscription email when data is refreshed." The "Subscription schedule: <schedule name>." line was removed on 2026-10-27 at the user's request; the mail no longer states the schedule.
- Title and wording for view subscriptions ("to the view <name>", H1 = view name) and the view PDF file name.
- Whether Tableau itself caps or crops wide crosstab images, and which resolution/width it requests (sample images were 2100x2200, 2100x2500, 1800x2000, so roughly 2x of a ~1000 px dashboard).
- Whether Tableau's workbook image email includes every listed tab (assumed yes, from the sample's 3 of 3).
- Message text (`sub.message`) placement: shown under the H1 if present; none of the tested subscriptions had one.
- The hidden SES tracking pixel is deliberately omitted.

## Running it
```
python scripts/preview_smtp.py            # local test SMTP, writes .eml into out/
python scripts/preview_export.py --subscription-id <id> [--dry-run]
```
The script refuses any SMTP host other than localhost/127.0.0.1 and reads no SMTP/SES credential. It signs in with the PAT in .env; per D-017 the production script needs its own separate PAT.
