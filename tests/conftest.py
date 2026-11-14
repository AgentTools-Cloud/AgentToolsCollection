import pathlib

# Live-copy scripts run at import time: under pytest an earlier module has already bound
# directory.db to the default (production) path, so they must never be collected.
collect_ignore = sorted(p.name for p in pathlib.Path(__file__).parent.glob("test_*.py")
                        if "live_copy_guard" in p.read_text(encoding="utf-8", errors="ignore"))
