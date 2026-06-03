# -*- coding: utf-8 -*-
"""
Verificador Agent — GUI entry point (builds into Client Verificador.exe).

The user pastes their personal token (from the web app's "Minha Conta" page)
and clicks Connect.  The VPS addresses are hardcoded below — change them before
building the .exe.

Connects in parallel to:
  - Verificador (wss://verificador.verifywaba.store) — full job/profile/browser bridge
  - Gerenciador de BMs (wss://manager.verifywaba.store) — open_browser / browser_status only

Both use the same token. Two independent status dots in the UI.

Build — always use the skill, never build by hand:
    /build-verificador-agent
"""

# ── Persistent log sink (frozen exe only) ─────────────────────────────────────
# Must happen before ANY other import so that services/facebook_bot print() calls
# are captured.  In the windowed exe stdout/stderr are discarded; this redirects
# them to <exe-dir>/logs/agent_YYYYMMDD_HHMMSS.log so domain/lang/PDF traces are
# visible after a hang.
import sys
import os
from pathlib import Path
import datetime

if getattr(sys, "frozen", False):
    _log_dir = Path(sys.executable).parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_path = _log_dir / f"agent_{_ts}.log"
    _log_file_handle = open(_log_path, "w", encoding="utf-8", buffering=1)
    sys.stdout = _log_file_handle
    sys.stderr = _log_file_handle
    print(f"[AGENT] Log file: {_log_path}", flush=True)

# ── VPS addresses — CHANGE THESE before building the .exe ────────────────────
_VPS_WS_BASE = "wss://verificador.verifywaba.store"   # routed via nginx (port 443 TLS)
_BMS_WS_BASE = "wss://manager.verifywaba.store"

# ── Env vars (must be set before main.py is imported inside job threads) ──────
import os as _os
_vps_http = _VPS_WS_BASE.replace("wss://", "https://").replace("ws://", "http://")
_os.environ.setdefault("VPS_URL",          _vps_http)
_os.environ.setdefault("WORKER_API_KEY",   _os.getenv("WORKER_API_KEY", "change-this-secret-key"))

# ── Project path ──────────────────────────────────────────────────────────────
import asyncio
import json
import queue
import threading
import tkinter as tk

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ── AdsPower auto-detection ───────────────────────────────────────────────────

def _detect_adspower() -> str:
    import requests as _req
    for base in (
        "http://local.adspower.net:50365",
        "http://127.0.0.1:50365",
        "http://local.adspower.net:50325",
        "http://127.0.0.1:50325",
    ):
        try:
            _req.get(f"{base}/api/v1/status", timeout=2)
            return base
        except Exception:
            continue
    return "http://local.adspower.net:50325"


_ADSPOWER_BASE = _detect_adspower()

if getattr(sys, "frozen", False):
    _DEBUG_DIR = Path(sys.executable).parent / "debug"
else:
    _DEBUG_DIR = Path(__file__).parent / "debug"

# ── Bootstrap agent_core ──────────────────────────────────────────────────────

import agent_core
from services.adspower import AdsPowerClient

agent_core.init(
    adspower_client=AdsPowerClient(_ADSPOWER_BASE),
    debug_dir=_DEBUG_DIR,
)

# ── Colour palette (mirrors the web app's Tailwind theme) ────────────────────
BG       = "#09090b"
CARD     = "#18181b"
BORDER   = "#27272a"
TEXT     = "#f4f4f5"
MUTED    = "#71717a"
INDIGO   = "#6366f1"
INDIGO_D = "#4f46e5"
GREEN    = "#34d399"
RED      = "#f87171"
YELLOW   = "#fbbf24"
ENTRY_BG = "#0f0f11"
FONT_UI  = ("Segoe UI", 10)
FONT_MONO= ("Consolas", 9)
FONT_BIG = ("Segoe UI", 13, "bold")
FONT_SM  = ("Segoe UI", 9)


# ── Tkinter GUI ───────────────────────────────────────────────────────────────

class AgentApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Verificador Agent")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.geometry("540x500")

        try:
            self.root.iconbitmap(ROOT / "prosperidadelogo.ico")
        except Exception:
            pass

        self._log_queue:  queue.Queue = queue.Queue()
        self._stop_event: threading.Event = threading.Event()
        self._loop:       asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._thread:     threading.Thread | None = None

        self._conn_states: dict[str, str] = {"verif": "offline", "bms": "offline"}

        self._build_ui()
        self.root.after(100, self._poll_logs)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg=CARD, pady=14)
        hdr.pack(fill="x")

        logo_frame = tk.Frame(hdr, bg=CARD)
        logo_frame.pack(padx=20)

        logo_box = tk.Frame(logo_frame, bg=INDIGO, width=38, height=38)
        logo_box.pack(side="left")
        logo_box.pack_propagate(False)
        tk.Label(logo_box, text="V", bg=INDIGO, fg=TEXT,
                 font=("Segoe UI", 14, "bold")).place(relx=.5, rely=.5, anchor="center")

        title_frame = tk.Frame(logo_frame, bg=CARD)
        title_frame.pack(side="left", padx=(10, 0))
        tk.Label(title_frame, text="Verificador Agent", bg=CARD, fg=TEXT,
                 font=FONT_BIG, anchor="w").pack(anchor="w")
        tk.Label(title_frame, text="dark • clean • by day", bg=CARD, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(anchor="w")

        form = tk.Frame(self.root, bg=BG, padx=20, pady=20)
        form.pack(fill="x")

        tk.Label(form, text="Token", bg=BG, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 4))
        tk.Label(form,
                 text="Copie em: Verificador Web → Minha Conta → Token do Agent",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(0, 6))

        token_frame = tk.Frame(form, bg=BORDER)
        token_frame.pack(fill="x", pady=(0, 18))

        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(
            token_frame, textvariable=self.token_var,
            bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=FONT_MONO, bd=8, show="●",
        )
        self.token_entry.pack(fill="x", side="left", expand=True, ipady=2)

        self._show_token = False
        tk.Button(
            token_frame, text="👁", bg=ENTRY_BG, fg=MUTED,
            relief="flat", font=FONT_SM, bd=0, cursor="hand2",
            command=self._toggle_token,
        ).pack(side="right", padx=4)

        ctrl = tk.Frame(self.root, bg=BG, padx=20)
        ctrl.pack(fill="x")

        dots_frame = tk.Frame(ctrl, bg=CARD, padx=8, pady=8)
        dots_frame.pack(side="left", fill="y")

        self.dots: dict[str, tuple] = {}
        for key, label_text in (("verif", "Verificador"), ("bms", "BMs")):
            blk = tk.Frame(dots_frame, bg=CARD, padx=6)
            blk.pack(side="left")

            canvas = tk.Canvas(blk, width=10, height=10, bg=CARD, highlightthickness=0)
            canvas.pack(side="left", padx=(0, 4))
            dot_item = canvas.create_oval(1, 1, 9, 9, fill=MUTED, outline="")

            lbl = tk.Label(blk, text=label_text, bg=CARD, fg=MUTED, font=FONT_UI)
            lbl.pack(side="left")

            if key == "verif":
                tk.Label(dots_frame, text="│", bg=CARD, fg=BORDER, font=FONT_UI).pack(side="left", padx=4)

            self.dots[key] = (canvas, dot_item, lbl)

        self.connect_btn = tk.Button(
            ctrl, text="Conectar",
            bg=INDIGO, fg=TEXT, activebackground=INDIGO_D, activeforeground=TEXT,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=20, pady=8, cursor="hand2",
            command=self._on_connect_click,
        )
        self.connect_btn.pack(side="right")

        log_outer = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        log_outer.pack(fill="both", expand=True)

        tk.Label(log_outer, text="Log", bg=BG, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 6))

        log_frame = tk.Frame(log_outer, bg=CARD)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame, bg=CARD, fg=TEXT, insertbackground=TEXT,
            font=FONT_MONO, relief="flat", state="disabled",
            wrap="word", bd=8,
        )
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview,
                          bg=BORDER, troughcolor=CARD, relief="flat")
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        self.log_text.tag_configure("ok",    foreground=GREEN)
        self.log_text.tag_configure("err",   foreground=RED)
        self.log_text.tag_configure("warn",  foreground=YELLOW)
        self.log_text.tag_configure("info",  foreground=TEXT)
        self.log_text.tag_configure("muted", foreground=MUTED)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "●")

    def _set_status(self, which: str, state: str):
        self._conn_states[which] = state
        cfg = {
            "online":     (GREEN,  "Online"),
            "offline":    (MUTED,  "Desconectado"),
            "connecting": (YELLOW, "Conectando…"),
        }
        colour, label_suffix = cfg.get(state, (MUTED, state))
        self.root.after(0, lambda: self._apply_dot(which, colour, label_suffix, state))

    def _apply_dot(self, which: str, colour: str, label_suffix: str, state: str):
        canvas, dot_item, lbl = self.dots[which]
        canvas.itemconfig(dot_item, fill=colour)
        lbl.config(fg=colour)

        any_alive = any(
            s in ("online", "connecting") for s in self._conn_states.values()
        )
        if any_alive:
            self.connect_btn.config(text="Desconectar",
                                    bg="#7f1d1d", activebackground="#991b1b")
        else:
            self.connect_btn.config(text="Conectar",
                                    bg=INDIGO, activebackground=INDIGO_D)

    def log(self, msg: str):
        # Write to the Tk queue AND to the log file (print goes to file via redirect above)
        self._log_queue.put(msg)
        print(msg, flush=True)

    def _poll_logs(self):
        while not self._log_queue.empty():
            self._append_log(self._log_queue.get_nowait())
        self.root.after(100, self._poll_logs)

    def _append_log(self, msg: str):
        ml  = msg.lower()
        tag = "info"
        if "✓" in msg or "sucesso" in ml or "conectado" in ml:
            tag = "ok"
        elif "✗" in msg or "erro" in ml or "falha" in ml or "exceção" in ml:
            tag = "err"
        elif "reconectando" in ml or "desconectado" in ml:
            tag = "warn"
        elif msg.startswith("[SYNC]") or "periódico" in ml:
            tag = "muted"

        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_connect_click(self):
        if self._thread and self._thread.is_alive():
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        token = self.token_var.get().strip()
        if not token:
            self._append_log("[ERRO] Cole seu token antes de conectar.")
            return

        verif_url = f"{_VPS_WS_BASE.rstrip('/')}/agent/ws?token={token}"
        bms_url   = f"{_BMS_WS_BASE.rstrip('/')}/agent/ws?token={token}"

        self._stop_event.clear()
        self.token_entry.config(state="disabled")
        self._thread = threading.Thread(
            target=self._run_loop, args=(verif_url, bms_url), daemon=True
        )
        self._thread.start()

    def _disconnect(self):
        self._stop_event.set()
        if self._async_stop and self._loop:
            self._loop.call_soon_threadsafe(self._async_stop.set)
        self.token_entry.config(state="normal")

    def _run_loop(self, verif_url: str, bms_url: str):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._async_stop = asyncio.Event()
        try:
            self._loop.run_until_complete(
                agent_core.connect_loop(
                    verif_url=verif_url,
                    bms_url=bms_url,
                    log=self.log,
                    on_status=self._set_status,
                    stop=self._async_stop,
                )
            )
        finally:
            self._loop.close()
            self._loop = None
            self._async_stop = None
            self.root.after(0, lambda: self.token_entry.config(state="normal"))

    def _on_close(self):
        self._disconnect()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AgentApp().run()
