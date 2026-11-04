"""Copy the git-tracked files to a folder and replace internal values with placeholders, for publishing.

    python scripts/make_public_copy.py TARGET_DIR

The substitution map is publish_map.local.json in the repo root (gitignored): {"real value": "placeholder", ...}.
Matching is case-insensitive, longest key first. Only tracked files are copied (git ls-files); binary files are copied as they are.
The working tree is never changed.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_FILE = ROOT / "publish_map.local.json"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    target = Path(sys.argv[1]).resolve()
    if ROOT in target.parents or target == ROOT:
        sys.exit("Refusing: the target folder must be outside the repo")
    if target.exists() and any(target.iterdir()):
        sys.exit("Refusing: %s is not empty" % target)
    if not MAP_FILE.exists():
        sys.exit("Missing %s (keys = real value, values = placeholder)" % MAP_FILE.name)
    mapping = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in keys), re.IGNORECASE)
    lookup = {k.lower(): v for k, v in mapping.items()}
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    n_changed = 0
    for rel in files:
        src, dst = ROOT / rel, target / rel
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw = src.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(src, dst)
            continue
        new = pattern.sub(lambda m: lookup[m.group(0).lower()], text)
        n_changed += new != text
        dst.write_bytes(new.encode("utf-8"))   # bytes: keeps CRLF/LF exactly as in the working tree
    print("copied %d files to %s (%d changed by the map)" % (len(files), target, n_changed))


if __name__ == "__main__":
    main()
