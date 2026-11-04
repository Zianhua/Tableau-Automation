"""Local test SMTP server for preview_export.py: writes every received message as .eml into --out-dir.
Binds to 127.0.0.1 only. Usage: python scripts/preview_smtp.py [--port 1025] [--out-dir out]
"""
import argparse
import asyncio
import time
from pathlib import Path

from aiosmtpd.controller import Controller

ROOT = Path(__file__).resolve().parent.parent


class EmlHandler:
    def __init__(self, out):
        self.out = out

    async def handle_DATA(self, server, session, envelope):
        self.out.mkdir(parents=True, exist_ok=True)
        f = self.out / ("smtp_%s_%03d.eml" % (time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000))
        f.write_bytes(envelope.original_content)
        print("received for %s -> %s" % (envelope.rcpt_tos, f.name), flush=True)
        return "250 OK"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1025)
    ap.add_argument("--out-dir", default=str(ROOT / "out"))
    a = ap.parse_args()
    Controller(EmlHandler(Path(a.out_dir)), hostname="127.0.0.1", port=a.port).start()
    print("listening on 127.0.0.1:%d" % a.port, flush=True)
    asyncio.new_event_loop().run_forever()
