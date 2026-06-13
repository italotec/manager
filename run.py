import os

# TEMP (leak hunting): start allocation tracing before anything else allocates,
# so tracemalloc can attribute growth to the exact source line. Gated by env var
# (TRACEMALLOC=<frame depth>) so it can be toggled via systemd without a redeploy.
if os.environ.get("TRACEMALLOC"):
    import tracemalloc
    tracemalloc.start(int(os.environ.get("TRACEMALLOC", "10")))

from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=False disables the auto-reloader (which restarts on any file change and
    # kills in-flight background job threads) and closes the Werkzeug debugger RCE
    # hole. threaded=True keeps concurrent request handling; the webhook now ACKs
    # instantly so request threads no longer pile up.
    app.run(host="0.0.0.0", port=5003, debug=False, use_reloader=False, threaded=True)
