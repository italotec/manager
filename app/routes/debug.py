"""
debug.py — TEMPORARY leak-hunting instrumentation (token-gated).

Remove this blueprint once the memory leak is found and fixed. It exposes a
read-only snapshot of process memory; gated by a secret token so it is not a
public information leak. It changes NO application behavior.
"""
import gc
import sys
import threading
import traceback
from collections import Counter

from flask import Blueprint, request, jsonify

try:
    import tracemalloc
except Exception:  # pragma: no cover
    tracemalloc = None

bp = Blueprint("debug", __name__)

# Secret gate — only requests carrying ?t=<this> get a response.
_TOKEN = "leakhunt-7Kq2pX9"


def _authed() -> bool:
    return request.args.get("t", "") == _TOKEN


def _type_histogram(limit: int = 30):
    """Count live Python objects by type name — biggest leak signal."""
    counts: Counter = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return [{"type": name, "count": n} for name, n in counts.most_common(limit)]


@bp.route("/debug/mem")
def mem():
    if not _authed():
        return "Forbidden", 403

    out = {
        "rss_mb": _rss_mb(),
        "threads": threading.active_count(),
        "thread_names": _thread_name_histogram(),
        "gc_objects_total": len(gc.get_objects()),
        "gc_counts": gc.get_count(),
        "gc_garbage": len(gc.garbage),
        "type_histogram": _type_histogram(int(request.args.get("types", 30))),
    }

    if tracemalloc is not None and tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot()
        group_by = request.args.get("group", "lineno")  # lineno | traceback
        top_n = int(request.args.get("top", 25))
        stats = snap.statistics(group_by)[:top_n]
        cur, peak = tracemalloc.get_traced_memory()
        out["tracemalloc"] = {
            "traced_current_mb": round(cur / 1048576, 2),
            "traced_peak_mb": round(peak / 1048576, 2),
            "top": [
                {
                    "size_mb": round(s.size / 1048576, 3),
                    "count": s.count,
                    "where": [str(f) for f in s.traceback.format()],
                }
                for s in stats
            ],
        }
    else:
        out["tracemalloc"] = "not tracing (set TRACEMALLOC env var to enable)"

    return jsonify(out)


@bp.route("/debug/threads")
def threads():
    if not _authed():
        return "Forbidden", 403
    frames = sys._current_frames()
    dump = {}
    for thread in threading.enumerate():
        fr = frames.get(thread.ident)
        dump[f"{thread.name}#{thread.ident}"] = (
            traceback.format_stack(fr) if fr else ["<no frame>"]
        )
    return jsonify({"count": threading.active_count(), "stacks": dump})


def _thread_name_histogram():
    counts: Counter = Counter()
    for t in threading.enumerate():
        # strip trailing numeric suffix so "Thread-1234" groups as "Thread-N"
        base = t.name.rstrip("0123456789") or t.name
        counts[base] += 1
    return dict(counts)


def _rss_mb():
    try:
        with open(f"/proc/{__import__('os').getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return None
