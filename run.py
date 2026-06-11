from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=False disables the auto-reloader (which restarts on any file change and
    # kills in-flight background job threads) and closes the Werkzeug debugger RCE
    # hole. threaded=True keeps concurrent request handling; the webhook now ACKs
    # instantly so request threads no longer pile up.
    app.run(host="0.0.0.0", port=5003, debug=False, use_reloader=False, threaded=True)
