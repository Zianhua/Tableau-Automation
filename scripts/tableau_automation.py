"""Entry point of the packaged build: bin\\tableau_automation.exe <command> [arguments].

    tableau_automation.exe t4_refresh --dry-run        is the same run as        python scripts\\t4_refresh.py --dry-run

Each command runs its script unchanged, as __main__, with the same arguments and exit code. The install folder is found the
same way as for the .py scripts (two levels above the module file): bin\\ holds the program, so <install folder>\\.env and
<install folder>\\out\\ are used, exactly as before. Build it with scripts\\build_exe.ps1.

    tableau_automation.exe selfcheck                    prints the install folder and the packages, and checks that bin\\ was
                                                        built from the scripts\\*.py next to it; changes nothing
"""
import hashlib
import runpy
import sys
from pathlib import Path

COMMANDS = ("t2_mapping_sync", "extract_takeover", "t11_pause", "t4_refresh", "t6_send", "watchdog", "daily_summary",
            "review_list")
NOT_SHIPPED = ("check_fixtures.py", "testguard.py")   # with *_checks.py: never copied to a host (install_on_host.ps1)


def source_hashes(scripts_dir):
    """{file name: sha256} of the scripts that can be deployed (line endings normalised: a GitLab download and a Windows
    checkout agree)."""
    out = {}
    for f in Path(scripts_dir).glob("*.py"):
        if not (f.name.endswith("_checks.py") or f.name in NOT_SHIPPED):
            out[f.name.lower()] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def stale_build(root):
    """None when every scripts\\*.py next to the program is the one it was built from, else the reason. A dev-only script
    that is not deployed (in the build, missing here) is fine."""
    stamp = Path(sys.executable).resolve().parent / "SOURCE_HASH.txt"
    if not stamp.is_file():
        return "bin\\SOURCE_HASH.txt is missing (build with scripts\\build_exe.ps1)"
    if not (root / "scripts").is_dir():
        return None
    built = dict(line.split(None, 1)[::-1] for line in stamp.read_text(encoding="ascii").splitlines() if line.strip())
    bad = sorted(n for n, h in source_hashes(root / "scripts").items() if built.get(n) != h)
    if bad:
        return ("bin\\ was not built from these scripts in %s\\scripts: %s. Rebuild (scripts\\build_exe.ps1) and copy bin\\ "
                "and scripts\\ together" % (root, ", ".join(bad)))
    return None


def selfcheck():
    import ops   # computes ROOT exactly like every script
    import dotenv, pyodbc, sqlglot, tableauserverclient, PIL  # noqa: F401  (all packages are in the build)
    expected = Path(sys.executable).resolve().parent.parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    print("install folder: %s" % ops.ROOT)
    print(".env present:   %s" % (ops.ROOT / ".env").is_file())
    print("ODBC drivers:   %s" % ", ".join(d for d in pyodbc.drivers() if "SQL Server" in d))
    print("packages:       pyodbc %s, sqlglot %s, Pillow %s (tableauserverclient, python-dotenv loaded)"
          % (pyodbc.version, sqlglot.__version__, PIL.__version__))
    if ops.ROOT != expected:
        print("FAIL: the scripts would use %s, expected %s (bin\\ must sit directly in the install folder)" % (ops.ROOT, expected))
        return 1
    if getattr(sys, "frozen", False):
        reason = stale_build(ops.ROOT)
        if reason:
            print("FAIL: " + reason)
            return 1
        print("build matches:  scripts\\*.py")
    print("OK")
    return 0


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "sourcehash" and not getattr(sys, "frozen", False):   # used by build_exe.ps1
        for name, h in sorted(source_hashes(Path(__file__).resolve().parent).items()):
            print("%s  %s" % (h, name))   # sha256sum format
        return 0
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS + ("selfcheck",):
        print(__doc__)
        print("commands: " + ", ".join(COMMANDS + ("selfcheck",)))
        return 2
    name = sys.argv[1]
    if name == "selfcheck":
        return selfcheck()
    sys.argv = [name + ".py"] + sys.argv[2:]
    runpy.run_module(name, run_name="__main__", alter_sys=True)   # the script's own sys.exit(main()) sets the exit code
    return 0


if __name__ == "__main__":
    sys.exit(main())
