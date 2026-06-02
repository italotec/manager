import json
import queue
import threading
import uuid
from dataclasses import dataclass, field

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from ..models import User
from .. import db

bp = Blueprint("agent_ws", __name__, url_prefix="/agent")


# ── Per-user agent registry ───────────────────────────────────────────────────

@dataclass
class AgentSession:
    user_id:       int
    username:      str
    ws:            object
    send_queue:    queue.Queue = field(default_factory=queue.Queue)
    sender_thread: object = None


_registry_lock = threading.Lock()
_agents: dict[int, AgentSession] = {}

_open_browsers: dict[int, set[str]] = {}
_open_browsers_lock = threading.Lock()

# ── Command/result correlation (request-response over the WS) ─────────────────

_pending: dict[str, queue.Queue] = {}
_pending_lock = threading.Lock()


def send_command_and_wait(user_id: int, msg: dict, timeout: float = 120.0) -> dict:
    """Push a command to the agent and block until it replies (or times out).

    The caller must set msg["type"]; this function injects a unique cmd_id and
    registers a reply queue before sending so no race with the receive loop.
    """
    if not is_agent_connected(user_id):
        return {"ok": False, "error": "agente não conectado"}

    cmd_id = str(uuid.uuid4())
    msg = {**msg, "cmd_id": cmd_id}
    reply_q: queue.Queue = queue.Queue()

    with _pending_lock:
        _pending[cmd_id] = reply_q

    try:
        if not push_to_agent(user_id, msg):
            return {"ok": False, "error": "agente desconectou antes do envio"}
        try:
            result = reply_q.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "timeout — agente não respondeu"}
        return result
    finally:
        with _pending_lock:
            _pending.pop(cmd_id, None)


def get_open_profiles(user_id: int) -> set[str]:
    with _open_browsers_lock:
        return set(_open_browsers.get(user_id, ()))


def is_agent_connected(user_id: int) -> bool:
    return user_id in _agents


def push_to_agent(user_id: int, msg: dict) -> bool:
    session = _agents.get(user_id)
    if not session:
        return False
    session.send_queue.put(json.dumps(msg))
    return True


# ── Auth ───────────────────────────────────────────────────────────────────────

def _auth_user() -> User | None:
    token = request.args.get("token", "").strip()
    if not token:
        return None
    return User.query.filter_by(agent_token=token).first()


# ── Incoming message handlers ─────────────────────────────────────────────────

def _handle_browser_status(user_id: int, open_profile_ids: list):
    with _open_browsers_lock:
        _open_browsers[user_id] = set(open_profile_ids)


def _handle_agent_message(user_id: int, data: str):
    try:
        msg = json.loads(data)
    except Exception:
        return
    msg_type = msg.get("type", "")
    if msg_type == "browser_status":
        _handle_browser_status(user_id, msg.get("open_profile_ids", []))
    elif msg_type == "card_result":
        cmd_id = msg.get("cmd_id", "")
        with _pending_lock:
            q = _pending.get(cmd_id)
        if q:
            q.put(msg)
    # "ping" and unknown types → silently ignored


# ── WebSocket handler (registered via sock.route in __init__.py) ──────────────

def handle_ws(ws):
    user = _auth_user()
    if not user:
        print("[AGENT WS] Auth failed — invalid or missing token")
        ws.close()
        return

    user_id  = user.id
    username = user.username
    print(f"[AGENT WS] Auth OK — user='{username}' id={user_id}")

    session = AgentSession(user_id=user_id, username=username, ws=ws)

    with _registry_lock:
        old = _agents.get(user_id)
        if old:
            print(f"[AGENT WS] Closing previous session for '{username}'")
            try:
                old.ws.close()
            except Exception:
                pass
            old.send_queue.put(None)

        while not session.send_queue.empty():
            try:
                session.send_queue.get_nowait()
            except queue.Empty:
                break

        _agents[user_id] = session

    print(f"[AGENT WS] '{username}' registered")

    # ── Sender thread ─────────────────────────────────────────────────────────
    def _sender():
        while True:
            try:
                data = session.send_queue.get(timeout=1)
                if data is None:
                    break
                ws.send(data)
            except queue.Empty:
                if user_id not in _agents:
                    break
            except Exception as e:
                print(f"[AGENT WS] sender error: {type(e).__name__}: {e}")
                break

    session.sender_thread = threading.Thread(target=_sender, daemon=True)
    session.sender_thread.start()

    # ── Receive loop ──────────────────────────────────────────────────────────
    try:
        while True:
            try:
                data = ws.receive(timeout=120)
            except Exception as e:
                print(f"[AGENT WS] ws.receive() raised {type(e).__name__}: {e}")
                break
            if data is None:
                # timeout — send keepalive ping
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception as e:
                    print(f"[AGENT WS] ping send failed: {type(e).__name__}: {e}")
                    break
                continue
            try:
                _handle_agent_message(user_id, data)
            except Exception as e:
                print(f"[AGENT WS] message handling error: {type(e).__name__}: {e}")
    finally:
        with _registry_lock:
            if _agents.get(user_id) is session:
                del _agents[user_id]
        with _open_browsers_lock:
            _open_browsers.pop(user_id, None)
        session.send_queue.put(None)
        print(f"[AGENT WS] '{username}' disconnected")


# ── Status endpoint ───────────────────────────────────────────────────────────

@bp.route("/status")
@login_required
def agent_status():
    return jsonify({"online": is_agent_connected(current_user.id)})
