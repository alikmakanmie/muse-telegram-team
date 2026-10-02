#!/usr/bin/env python3
"""Muse bridge v5: OpenAI-compatible endpoint + worker pull API (no SSH).

9Router 'muse' provider -> http://127.0.0.1:8765/v1      (key role=user)
Muse worker (sandbox)   -> https://<tunnel-url>/muse/*    (key role=worker)
                            tunnel via cloudflared quick tunnel (run-tunnel.ps1)

v5 (new):
- Role-based API keys stored in keys.json (NOT printed except at creation):
    python bridge.py keygen --role worker --label muse-vm   # prints key ONCE
    python bridge.py keygen --role user   --label 9router
    python bridge.py keylist                 # label/role/created/key-prefix only
    python bridge.py keydel <prefix|label>    # revoke
- Worker pull API (all require `Authorization: Bearer <worker-key>`):
    GET  /muse/pending?limit=3   -> atomically leases up to `limit` jobs
                                   (lease = LEASE_SECS, default 180s);
                                   {"jobs":[{id,received_at,request}],"count":N,"pending":M}
    POST /muse/answer  {"id","content"} -> completes the job; the waiting
                                          /v1/chat/completions returns it
    POST /muse/release {"id"}           -> releases the lease early; the job
                                          becomes pending again
- Expired leases are returned to pending/ automatically by the sweeper.
- /health stays open (no auth) for tunnel/monitoring checks.

v4 (kept):
- Optional shared-token auth: set BRIDGE_TOKEN env. Still accepted on /v1/*
  as a legacy user-role key (so an existing 9Router config keeps working).
- Request body cap (HTTP 413 over MAX_BODY).
- processing/ dir + sweeper thread: stale/expired processing/ jobs are
  requeued, orphan done/ files are deleted.
- Threaded server; SSE streaming with immediate headers + keepalives.
- Dashboard probe shortcut ("hi" / max_tokens=1024 / non-streaming).

Queue layout (under BRIDGE_QUEUE, default /home/ubuntu/muse-bridge/queue):
  pending/<id>.json      unleased jobs
  processing/<id>.json   leased jobs (contain "_lease":{"by","until"})
  done/<id>.json         answered jobs (contain "content")
"""
import argparse
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

QUEUE = os.environ.get("BRIDGE_QUEUE", "/home/ubuntu/muse-bridge/queue").rstrip("/")
PENDING = f"{QUEUE}/pending"
DONE = f"{QUEUE}/done"
PROCESSING = f"{QUEUE}/processing"
FAILED = f"{QUEUE}/failed"  # dead-letter: jobs yang gagal > MAX_ATTEMPTS kali
KEYS_FILE = os.environ.get("BRIDGE_KEYS",
                           os.path.join(os.path.dirname(QUEUE), "keys.json"))
MAX_PENDING = 5
WAIT_SECS = 600        # how long /v1/chat/completions waits for an answer
LEASE_SECS = int(os.environ.get("BRIDGE_LEASE_SECS", "180"))  # worker lease
MAX_ATTEMPTS = 3       # lease expiry > ini -> pindah ke failed/, tidak loop selamanya
MAX_HISTORY_MSGS = 15      # potong agresif demi kecepatan (1 Okt 2026)
MAX_HISTORY_CHARS = 30000  # ~7.5k tokens; konteks kecil = worker baca lebih cepat
KEEPALIVE_SECS = 15
MAX_BODY = 10 * 1024 * 1024  # 10 MB per request body
TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()  # legacy user key on /v1/*
DONE_ORPHAN_SECS = 600
PROCESSING_STALE_SECS = 600  # fallback when a job has no/invalid lease info

LOCK = threading.Lock()
RECENT_DONE = {}  # job id -> timestamp of completion (for duplicate-answer detection)
RECENT_DONE_TTL = 3600

# Statistik usage kumulatif (in-memory, reset saat bridge restart)
USAGE_STATS = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
               "total_tokens": 0, "by_model": {}}


def _record_usage(model, prompt_tokens, completion_tokens):
    USAGE_STATS["requests"] += 1
    USAGE_STATS["prompt_tokens"] += prompt_tokens
    USAGE_STATS["completion_tokens"] += completion_tokens
    USAGE_STATS["total_tokens"] += prompt_tokens + completion_tokens
    m = USAGE_STATS["by_model"].setdefault(
        model, {"requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0})
    m["requests"] += 1
    m["prompt_tokens"] += prompt_tokens
    m["completion_tokens"] += completion_tokens
    m["total_tokens"] += prompt_tokens + completion_tokens


def _usage_dashboard_html():
    """Halaman dashboard usage sederhana untuk Dhodi."""
    s = USAGE_STATS
    rows = ""
    for model, m in sorted(s["by_model"].items(),
                           key=lambda kv: -kv[1]["total_tokens"]):
        rows += (f"<tr><td><code>{model}</code></td><td>{m['requests']}</td>"
                 f"<td>{m['prompt_tokens']:,}</td>"
                 f"<td>{m['completion_tokens']:,}</td>"
                 f"<td><b>{m['total_tokens']:,}</b></td></tr>")
    if not rows:
        rows = ('<tr><td colspan="5" style="text-align:center;color:#888">'
                'Belum ada request tercatat.</td></tr>')
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dhodi — Token Usage</title>
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
h1{{font-size:1.4em}} .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:14px 18px;min-width:140px}}
.card .v{{font-size:1.6em;font-weight:700}} .card .l{{color:#999;font-size:.8em}}
table{{border-collapse:collapse;width:100%;max-width:720px;background:#1c1c1c;border-radius:10px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:right;border-bottom:1px solid #2a2a2a}}
th{{color:#999;font-weight:600}} td:first-child,th:first-child{{text-align:left}}
.note{{color:#888;font-size:.85em;margin-top:16px;max-width:720px}}
a{{color:#7ab8ff}}
</style></head><body>
<h1>&#x1F4CA; Dhodi — Token Usage</h1>
<div class="cards">
<div class="card"><div class="v">{s['requests']}</div><div class="l">Total Requests</div></div>
<div class="card"><div class="v">{s['prompt_tokens']:,}</div><div class="l">Prompt Tokens</div></div>
<div class="card"><div class="v">{s['completion_tokens']:,}</div><div class="l">Completion Tokens</div></div>
<div class="card"><div class="v">{s['total_tokens']:,}</div><div class="l">Total Tokens</div></div>
</div>
<table><tr><th>Model</th><th>Requests</th><th>Prompt</th><th>Completion</th><th>Total</th></tr>
{rows}</table>
<p class="note">Estimasi kasar (~4 karakter per token), bukan hitungan tokenizer asli.
Data in-memory &mdash; reset setiap bridge restart.
JSON: <a href="/v1/usage">/v1/usage</a></p>
<script>setTimeout(()=>location.reload(),30000)</script>
</body></html>"""


def _trim_history(req):
    """Potong messages agar worker tidak membaca ulang history raksasa
    setiap round. Simpan system message + N pesan terakhir; jika masih
    melebihi budget karakter, potong konten pesan tool terpanjang."""
    msgs = req.get("messages")
    if not isinstance(msgs, list) or len(msgs) <= MAX_HISTORY_MSGS:
        return req
    system = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"][:1]
    rest = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
    trimmed = system + rest[-MAX_HISTORY_MSGS:]
    # Jangan pisahkan pasangan tool_calls <-> hasil tool:
    # jika pesan tertua yang disimpan adalah hasil tool, tarik mundur
    # sampai mencakup pesan assistant yang memanggilnya.
    if len(rest) > MAX_HISTORY_MSGS:
        start_idx = len(rest) - MAX_HISTORY_MSGS
        while start_idx > 0:
            oldest = rest[start_idx]
            if isinstance(oldest, dict) and oldest.get("role") == "tool":
                start_idx -= 1
            else:
                break
        trimmed = system + rest[start_idx:]
    total = sum(len(str(m.get("content", ""))) for m in trimmed if isinstance(m, dict))
    if total > MAX_HISTORY_CHARS:
        # Potong dari pesan tool terlama dulu (hasil lama, paling tidak relevan)
        over = total - MAX_HISTORY_CHARS
        for m in trimmed:
            if over <= 0:
                break
            if isinstance(m, dict) and m.get("role") == "tool":
                c = str(m.get("content", ""))
                if len(c) > 2000:
                    cut = min(len(c) - 2000, over)
                    m["content"] = c[:len(c) - cut] + "\n...[dipotong]"
                    over -= cut
    req = dict(req)
    req["messages"] = trimmed
    req["_history_trimmed"] = len(msgs) - len(trimmed)
    return req


# ---------------------------------------------------------------- keys ---
def _load_keys():
    try:
        with open(KEYS_FILE) as f:
            return json.load(f).get("keys", [])
    except Exception:
        return []


def _save_keys(keys):
    os.makedirs(os.path.dirname(KEYS_FILE) or ".", exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"keys": keys}, f, indent=2)
    os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def cmd_keygen(role, label):
    if role not in ("user", "worker"):
        print("role must be 'user' or 'worker'", file=sys.stderr)
        return 2
    key = "Muse_kutkey_" + secrets.token_urlsafe(18)
    keys = _load_keys()
    keys.append({"key": key, "role": role, "label": label,
                 "created": datetime.now(timezone.utc).isoformat()})
    _save_keys(keys)
    print(key)  # full key shown ONLY here
    print(f"role={role} label={label} saved to {KEYS_FILE} "
          f"(full key is shown only this once)", file=sys.stderr)
    return 0


def cmd_keylist():
    keys = _load_keys()
    if not keys:
        print("(no keys yet — run: python bridge.py keygen --role worker --label muse-vm)")
        return 0
    print(f"{'LABEL':<20}{'ROLE':<8}{'CREATED':<33}PREFIX")
    for k in keys:
        print(f"{k.get('label',''):<20}{k.get('role',''):<8}"
              f"{k.get('created',''):<33}{k.get('key','')[:14]}...")
    return 0


def cmd_keydel(ident):
    keys = _load_keys()
    keep = [k for k in keys
            if not (k.get("key", "").startswith(ident) or k.get("label") == ident)]
    removed = len(keys) - len(keep)
    if removed:
        _save_keys(keep)
    print(f"removed {removed} key(s)")
    return 0


# --------------------------------------------------------------- queue ---
def _job_path(d, jid):
    # jid is hex from uuid4; guard against path traversal anyway
    safe = "".join(c for c in jid if c.isalnum())[:64]
    return os.path.join(d, safe + ".json")


def _claim_jobs(limit, worker_label):
    """Atomically move up to `limit` pending jobs to processing/ with a lease."""
    claimed, now = [], time.time()
    with LOCK:
        try:
            files = sorted(os.listdir(PENDING))
        except Exception:
            files = []
        for fn in files:
            if len(claimed) >= limit or not fn.endswith(".json"):
                continue
            src, dst = os.path.join(PENDING, fn), os.path.join(PROCESSING, fn)
            try:
                os.rename(src, dst)  # atomic claim within one filesystem
            except FileNotFoundError:
                continue
            try:
                with open(dst) as f:
                    job = json.load(f)
            except Exception:
                job = {"id": fn[:-5]}
            job["_lease"] = {"by": worker_label, "until": now + LEASE_SECS}
            tmp = dst + ".tmp"
            with open(tmp, "w") as f:
                json.dump(job, f)
            os.replace(tmp, dst)
            claimed.append({"id": job.get("id", fn[:-5]),
                            "received_at": job.get("received_at"),
                            "request": job.get("request", {})})
        try:
            pending_n = sum(1 for f in os.listdir(PENDING) if f.endswith(".json"))
        except Exception:
            pending_n = 0
    return claimed, pending_n


def _partial_path(jid):
    return _job_path(PROCESSING, jid) + ".partial"


def _read_partial(jid):
    """Read accumulated partial content for streaming. Returns str."""
    try:
        with open(_partial_path(jid)) as f:
            return f.read()
    except Exception:
        return ""


def _answer_job(jid, payload):
    """payload: {"content": str, "tool_calls": [..] | None, "done": bool}.
    Jika done=False: append content ke partial stream (untuk streaming),
    return 'partial'. Jika tidak: finalize jawaban.
    Returns 'ok' | 'duplicate' | 'partial' | None (unknown id)."""
    with LOCK:
        p = _job_path(PROCESSING, jid)
        if os.path.exists(p):
            job_active, first = True, True
        elif os.path.exists(_job_path(DONE, jid)):
            job_active, first = True, False  # answered but not yet picked up
        elif jid in RECENT_DONE:
            return "duplicate"  # answered AND already delivered
        else:
            return None
        # Partial chunk: append ke partial file, job tetap di processing/
        if payload.get("done") is False:
            content = payload.get("content", "")
            if content:
                try:
                    with open(_partial_path(jid), "a") as f:
                        f.write(content)
                except Exception:
                    pass
            return "partial"
        # Final: gabung partial + content terakhir, pindah ke DONE
        partial_content = _read_partial(jid)
        try:
            os.remove(_partial_path(jid))
        except Exception:
            pass
        full_content = partial_content + payload.get("content", "")
        final_payload = {"content": full_content}
        if payload.get("tool_calls"):
            final_payload["tool_calls"] = payload["tool_calls"]
        try:
            os.remove(p)
        except Exception:
            pass
        dp = _job_path(DONE, jid)
        tmp = dp + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"answer": final_payload, "answered_at": time.time()}, f)
        os.replace(tmp, dp)
        RECENT_DONE[jid] = time.time()
    return "ok" if first else "duplicate"


def _validate_tool_calls(tool_calls):
    """Normalize/validate tool_calls from the worker. Returns list or raises ValueError."""
    if not isinstance(tool_calls, list) or not tool_calls:
        raise ValueError("tool_calls must be a non-empty list")
    out = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            raise ValueError("each tool_call must be an object")
        fn = tc.get("function")
        if not isinstance(fn, dict) or not fn.get("name"):
            raise ValueError("tool_call.function.name is required")
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            try:
                args = json.dumps(args)
            except Exception:
                raise ValueError("tool_call.function.arguments must be JSON-serializable")
        # validate that arguments is parseable JSON
        try:
            json.loads(args)
        except Exception:
            raise ValueError("tool_call.function.arguments must be a JSON string")
        out.append({
            "id": str(tc.get("id") or ("call_" + uuid.uuid4().hex[:12])),
            "type": "function",
            "function": {"name": str(fn["name"]), "arguments": args},
        })
    return out


def _release_job(jid):
    """Move a leased job back to pending/. Returns True if it was leased.
    Setiap release menaikkan _attempts; jika > MAX_ATTEMPTS, job dipindah
    ke failed/ (dead-letter) agar tidak loop selamanya dan kegagalan terlihat."""
    with LOCK:
        src = _job_path(PROCESSING, jid)
        if not os.path.exists(src):
            return False
        try:
            with open(src) as f:
                job = json.load(f)
        except Exception:
            job = {"id": jid}
        job.pop("_lease", None)
        attempts = int(job.get("_attempts", 0)) + 1
        job["_attempts"] = attempts
        if attempts > MAX_ATTEMPTS:
            os.makedirs(FAILED, exist_ok=True)
            dst = os.path.join(FAILED, jid + ".json")
            print(f"bridge: job {jid} -> failed/ setelah {attempts} percobaan",
                  flush=True)
        else:
            dst = _job_path(PENDING, jid)
            print(f"bridge: job {jid} dikembalikan ke antrean "
                  f"(percobaan {attempts}/{MAX_ATTEMPTS})", flush=True)
        tmp = dst + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.replace(tmp, dst)
        try:
            os.remove(src)
        except Exception:
            pass
    return True


def _sweep_once():
    """Delete orphan done/ files; return expired/stale processing/ jobs to pending/."""
    now = time.time()
    try:
        for fn in os.listdir(DONE):
            p = os.path.join(DONE, fn)
            try:
                if now - os.path.getmtime(p) > DONE_ORPHAN_SECS:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass
    try:
        for fn in os.listdir(PROCESSING):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(PROCESSING, fn)
            expired = False
            try:
                with open(p) as f:
                    job = json.load(f)
                until = (job.get("_lease") or {}).get("until")
                if isinstance(until, (int, float)) and now > until:
                    expired = True
                elif now - os.path.getmtime(p) > PROCESSING_STALE_SECS:
                    expired = True  # no/invalid lease info: stale fallback
            except Exception:
                expired = True
            if expired:
                _release_job(fn[:-5])
    except Exception:
        pass
    # prune duplicate-answer memory
    try:
        cutoff = now - RECENT_DONE_TTL
        for jid in [j for j, ts in RECENT_DONE.items() if ts < cutoff]:
            del RECENT_DONE[jid]
    except Exception:
        pass


def _sweeper():
    while True:
        time.sleep(60)
        _sweep_once()


# ------------------------------------------------------------- handler ---
def _is_dashboard_probe(req):
    """Detect 9Router dashboard's 'Test Connection' probe (15s client timeout)."""
    try:
        if not isinstance(req, dict) or req.get("stream"):
            return False
        if req.get("max_tokens") != 1024:
            return False
        msgs = req.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        return (isinstance(last, dict) and last.get("role") == "user"
                and str(last.get("content", "")).strip().lower() == "hi")
    except Exception:
        return False


def _probe_completion():
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content":
                                     "Halo! Muse online — bridge 9Router aktif dan siap."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class H(BaseHTTPRequestHandler):
    timeout = 120

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        try:
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def _role(self):
        """'user' | 'worker' | None from the Bearer key (or legacy BRIDGE_TOKEN)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        if TOKEN and hmac.compare_digest(token, TOKEN):
            return "user"  # legacy single token counts as a user key
        for k in _load_keys():
            if k.get("key") and hmac.compare_digest(token, k["key"]):
                return k.get("role")
        return None

    def _label(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        for k in _load_keys():
            if k.get("key") and hmac.compare_digest(token, k["key"]):
                return k.get("label", "?")
        return "token"

    def _require(self, role):
        if self._role() != role:
            self._send(401, {"error": {"message":
                f"unauthorized: need bearer key with role={role}",
                "type": "auth_error", "code": "invalid_api_key"}})
            return False
        return True

    def _require_user_dashboard(self):
        """Auth untuk dashboard HTML: Bearer header ATAU ?key= di URL
        (browser tidak bisa kirim Authorization header)."""
        if self._role() == "user":
            return True
        q = parse_qs(urlparse(self.path).query)
        key = (q.get("key") or [""])[0]
        if not key:
            self._send(401, {"error": {"message":
                "unauthorized: need bearer key or ?key=<user-key>",
                "type": "auth_error", "code": "invalid_api_key"}})
            return False
        # bandingkan key dari query param dengan key yang tersimpan
        if TOKEN and hmac.compare_digest(key, TOKEN):
            return True
        for k in _load_keys():
            if k.get("key") and hmac.compare_digest(key, k["key"]) \
                    and k.get("role") == "user":
                return True
        self._send(401, {"error": {"message": "unauthorized: invalid key",
            "type": "auth_error", "code": "invalid_api_key"}})
        return False

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._send(413, {"error": {"message":
                f"request body too large (>{MAX_BODY} bytes)"}})
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            self._send(400, {"error": {"message": "bad json"}})
            return None

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path.rstrip("/") == "/v1/models":
                if not self._require("user"):
                    return
                self._send(200, {"object": "list", "data": [
                    {"id": "muse", "object": "model", "created": 0,
                     "owned_by": "muse"},
                    {"id": "dhodi", "object": "model", "created": 0,
                     "owned_by": "dhodi"}]})
            elif path == "/health":
                self._send(200, {"ok": True})
            elif path.rstrip("/") == "/v1/usage":
                if not self._require("user"):
                    return
                self._send(200, {"object": "usage.stats",
                                 "note": "Estimasi kasar (~4 karakter per token), "
                                         "bukan hitungan tokenizer asli. "
                                         "Reset setiap bridge restart.",
                                 "data": USAGE_STATS})
            elif path.rstrip("/") == "/v1/usage/dashboard":
                if not self._require_user_dashboard():
                    return
                self._send(200, _usage_dashboard_html(), "text/html; charset=utf-8")
            elif path.rstrip("/") == "/muse/pending":
                if not self._require("worker"):
                    return
                try:
                    limit = int(parse_qs(urlparse(self.path).query)
                                    .get("limit", ["3"])[0])
                except Exception:
                    limit = 3
                limit = max(1, min(10, limit))
                jobs, pending_n = _claim_jobs(limit, self._label())
                self._send(200, {"jobs": jobs, "count": len(jobs),
                                 "pending": pending_n})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    # -- POST ----------------------------------------------------------
    def _cleanup(self, rid: str):
        for d in (PENDING, DONE):
            try:
                os.remove(_job_path(d, rid))
            except Exception:
                pass

    def _wait_answer(self, rid: str, keepalive_cb=None):
        deadline = time.time() + WAIT_SECS
        answer, last_ping = None, time.time()
        while time.time() < deadline:
            dp = _job_path(DONE, rid)
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        data = json.load(f)
                    # new format: {"answer": {"content":..,"tool_calls":..}}
                    # old format (transitional): {"content": ".."}
                    answer = data.get("answer")
                    if answer is None and "content" in data:
                        answer = {"content": data.get("content", ""),
                                  "tool_calls": None}
                except Exception:
                    answer = {"content": "", "tool_calls": None}
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            if keepalive_cb and time.time() - last_ping >= KEEPALIVE_SECS:
                if not keepalive_cb():
                    break
                last_ping = time.time()
            time.sleep(0.25)  # pickup DONE lebih sigap (2 Okt 2026)
        return answer

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/v1/chat/completions":
                if not self._require("user"):
                    return
                req = self._read_json()
                if req is None:
                    return
                if _is_dashboard_probe(req):
                    return self._send(200, _probe_completion())
                try:
                    npend = sum(1 for f in os.listdir(PENDING)
                                if f.endswith(".json"))
                except Exception:
                    npend = 0
                if npend >= MAX_PENDING:
                    return self._send(429, {"error": {"message":
                        "Muse bridge busy, try again in a bit"}})
                rid = uuid.uuid4().hex
                # History trimming AKTIF (1 Okt 2026, atas permintaan user untuk kecepatan):
                # pangkas ke 30 pesan terakhir agar worker tidak membaca history raksasa.
                req = _trim_history(req)
                with open(_job_path(PENDING, rid), "w") as f:
                    json.dump({"id": rid, "received_at": time.time(),
                               "request": req}, f)
                if req.get("stream"):
                    return self._handle_stream(req, rid)
                answer = self._wait_answer(rid)
                self._cleanup(rid)
                if answer is None:
                    return self._send(504, {"error": {"message":
                        "Muse did not answer in time"}})
                model = req.get("model") or "muse"
                prompt_tokens = _count_prompt_tokens(req)
                if answer.get("tool_calls"):
                    comp = _completion_tools(answer, model, prompt_tokens)
                else:
                    comp = _completion(answer.get("content", ""), model,
                                       prompt_tokens)
                _record_usage(model, prompt_tokens,
                              comp["usage"]["completion_tokens"])
                self._send(200, comp)
            elif path == "/muse/answer":
                if not self._require("worker"):
                    return
                body = self._read_json()
                if body is None:
                    return
                jid = body.get("id", "")
                content = body.get("content", "")
                tool_calls = body.get("tool_calls")
                if not jid or not isinstance(jid, str):
                    return self._send(400, {"error": {"message":
                        'need {"id": "...", "content": "..."} and/or "tool_calls": [...]'}})
                if not isinstance(content, str):
                    content = ""
                try:
                    if tool_calls is not None:
                        tool_calls = _validate_tool_calls(tool_calls)
                except ValueError as e:
                    return self._send(400, {"error": {"message": str(e)}})
                if not content and not tool_calls:
                    if body.get("done") is False:
                        # ACK/heartbeat murni tanpa payload: diterima & diabaikan
                        # (kompatibel dengan perilaku bridge lama yang dipatch).
                        self._send(200, {"ok": True, "id": jid, "ack": True})
                        return
                    return self._send(400, {"error": {"message":
                        "need non-empty content and/or tool_calls"}})
                res = _answer_job(jid, {"content": content,
                                        "tool_calls": tool_calls,
                                        "done": body.get("done", True)})
                if res is None:
                    return self._send(404, {"error": {"message":
                        "unknown job id (not leased?)"}})
                self._send(200, {"ok": True, "id": jid,
                                 "duplicate": res == "duplicate",
                                 "partial": res == "partial",
                                 "tool_calls": bool(tool_calls)})
            elif path == "/muse/release":
                if not self._require("worker"):
                    return
                body = self._read_json()
                if body is None:
                    return
                if _release_job(body.get("id", "")):
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": {"message":
                        "unknown job id (not leased?)"}})
            else:
                self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _handle_stream(self, req, rid: str):
        # PENTING: tutup koneksi setelah [DONE]. Klien SSE (undici/daemon)
        # membaca sampai koneksi tertutup — tanpa ini stream menggantung
        # sampai timeout meski jawaban sudah terkirim (fix 2 Okt 2026).
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self._cleanup(rid)
            return
        if not self._sse_write(": connected\n\n"):
            self._cleanup(rid)
            return
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        model = req.get("model") or "muse"
        # Kirim role chunk dulu agar klien tahu stream dimulai
        role_chunk = {"id": cid, "object": "chat.completion.chunk",
                      "created": created, "model": model,
                      "choices": [{"index": 0, "delta": {"role": "assistant"},
                                   "finish_reason": None}]}
        if not self._sse_write("data: " + json.dumps(role_chunk) + "\n\n"):
            self._cleanup(rid)
            return
        # Streaming loop: kirim partial content begitu tersedia,
        # selesai saat file DONE muncul atau timeout.
        deadline = time.time() + WAIT_SECS
        sent_len, last_ping = 0, time.time()
        answer = None
        while time.time() < deadline:
            dp = _job_path(DONE, rid)
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        data = json.load(f)
                    answer = data.get("answer")
                    if answer is None and "content" in data:
                        answer = {"content": data.get("content", ""),
                                  "tool_calls": None}
                except Exception:
                    answer = {"content": "", "tool_calls": None}
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            # Cek partial baru -> stream sebagai delta
            partial = _read_partial(rid)
            if len(partial) > sent_len:
                new_text = partial[sent_len:]
                sent_len = len(partial)
                chunk = {"id": cid, "object": "chat.completion.chunk",
                         "created": created, "model": model,
                         "choices": [{"index": 0,
                                      "delta": {"content": new_text},
                                      "finish_reason": None}]}
                if not self._sse_write("data: " + json.dumps(chunk) + "\n\n"):
                    self._cleanup(rid)
                    return
            if time.time() - last_ping >= KEEPALIVE_SECS:
                if not self._sse_write(": ping\n\n"):
                    self._cleanup(rid)
                    return
                last_ping = time.time()
            time.sleep(0.25)  # stream lebih responsif (2 Okt 2026)
        self._cleanup(rid)
        if answer is None:
            err = {"error": {"message": "Muse did not answer in time",
                             "type": "timeout"}}
            self._sse_write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
            return
        if answer.get("tool_calls"):
            self._sse_write_tool_calls(cid, created, model, answer, req)
            return
        # Kirim sisa content yang belum di-stream (final chunk dari worker)
        full_content = answer.get("content") or ""
        remaining = full_content[sent_len:]
        if remaining:
            chunk = {"id": cid, "object": "chat.completion.chunk",
                     "created": created, "model": model,
                     "choices": [{"index": 0, "delta": {"content": remaining},
                                  "finish_reason": None}]}
            self._sse_write("data: " + json.dumps(chunk) + "\n\n")
        chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": model, "choices": [{"index": 0, "delta": {},
                  "finish_reason": "stop"}]}
        self._sse_write("data: " + json.dumps(chunk2) + "\n\n")
        # Chunk usage ala OpenAI (stream_options.include_usage)
        prompt_tokens = _count_prompt_tokens(req)
        comp_tokens = _estimate_tokens(full_content)
        _record_usage(model, prompt_tokens, comp_tokens)
        usage_chunk = {"id": cid, "object": "chat.completion.chunk",
                       "created": created, "model": model, "choices": [],
                       "usage": _usage_dict(prompt_tokens, comp_tokens)}
        self._sse_write("data: " + json.dumps(usage_chunk) + "\n\ndata: [DONE]\n\n")

    def _sse_write_tool_calls(self, cid, created, model, answer, req=None):
        """Stream tool_calls the way OpenAI does: declare, then fill arguments."""
        tcs = answer.get("tool_calls") or []
        content = answer.get("content") or None
        # chunk 1: role + tool declarations (empty arguments)
        decls = []
        for i, tc in enumerate(tcs):
            decls.append({"index": i, "id": tc["id"], "type": "function",
                          "function": {"name": tc["function"]["name"],
                                       "arguments": ""}})
        delta1 = {"role": "assistant", "tool_calls": decls}
        if content:
            delta1["content"] = content
        self._sse_write("data: " + json.dumps(
            {"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": model, "choices": [{"index": 0, "delta": delta1,
             "finish_reason": None}]}) + "\n\n")
        # chunks 2..n: argument payloads per tool
        for i, tc in enumerate(tcs):
            self._sse_write("data: " + json.dumps(
                {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": model, "choices": [{"index": 0,
                 "delta": {"tool_calls": [{"index": i, "function":
                     {"arguments": tc["function"]["arguments"]}}]},
                 "finish_reason": None}]}) + "\n\n")
        # final chunk
        self._sse_write("data: " + json.dumps(
            {"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": model, "choices": [{"index": 0, "delta": {},
             "finish_reason": "tool_calls"}]}) + "\n\n")
        # Chunk usage ala OpenAI (stream_options.include_usage)
        comp_text = (content or "") + json.dumps(tcs)
        prompt_tokens = _count_prompt_tokens(req) if req else 0
        comp_tokens = _estimate_tokens(comp_text)
        _record_usage(model, prompt_tokens, comp_tokens)
        usage_chunk = {"id": cid, "object": "chat.completion.chunk",
                       "created": created, "model": model, "choices": [],
                       "usage": _usage_dict(prompt_tokens, comp_tokens)}
        self._sse_write("data: " + json.dumps(usage_chunk) + "\n\ndata: [DONE]\n\n")

    def _sse_write(self, payload: str) -> bool:
        try:
            self.wfile.write(payload.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False


def _estimate_tokens(text):
    """Estimasi kasar jumlah token dari teks (~4 karakter per token).
    Bukan hitungan tokenizer asli, tapi cukup untuk visibilitas usage."""
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


def _count_prompt_tokens(req):
    """Estimasi prompt_tokens dari messages + tools di request."""
    total = 0
    msgs = req.get("messages") or []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        total += _estimate_tokens(m.get("content", ""))
        # tool_calls di pesan assistant juga dihitung
        for tc in (m.get("tool_calls") or []):
            try:
                total += _estimate_tokens(json.dumps(tc))
            except Exception:
                pass
    for t in (req.get("tools") or []):
        try:
            total += _estimate_tokens(json.dumps(t))
        except Exception:
            pass
    # overhead format pesan OpenAI (~4 token per message)
    total += len(msgs) * 4
    return total


def _usage_dict(prompt_tokens, completion_tokens):
    return {"prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


def _completion(answer: str, model: str = "muse", prompt_tokens: int = 0):
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    comp_tokens = _estimate_tokens(answer)
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": _usage_dict(prompt_tokens, comp_tokens)}


def _completion_tools(answer: dict, model: str = "muse", prompt_tokens: int = 0):
    """OpenAI-format completion carrying tool_calls from the worker."""
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    content = answer.get("content") or None
    comp_text = (content or "") + json.dumps(answer.get("tool_calls") or [])
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": content,
                                     "tool_calls": answer.get("tool_calls")},
                         "finish_reason": "tool_calls"}],
            "usage": _usage_dict(prompt_tokens, _estimate_tokens(comp_text))}


# ------------------------------------------------------------------ ---
def cmd_serve():
    import subprocess
    for d in (PENDING, DONE, PROCESSING):
        os.makedirs(d, exist_ok=True)
    # crash recovery: anything left in processing/ goes back to pending/
    try:
        for fn in os.listdir(PROCESSING):
            if fn.endswith(".json"):
                _release_job(fn[:-5])
    except Exception:
        pass

    threading.Thread(target=_sweeper, daemon=True).start()

    def listen_addrs():
        addrs = ["127.0.0.1"]
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=10).stdout
            for ip in out.split():
                ip = ip.strip()
                if ip and ip not in addrs:
                    addrs.append(ip)
        except Exception:
            pass
        return addrs

    ok = 0
    for addr in listen_addrs():
        try:
            srv = ThreadingHTTPServer((addr, 8765), H)
        except OSError as e:
            print(f"  [!] tidak bisa listen di {addr}:8765 ({e})", flush=True)
            continue
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"muse-bridge v5 listening on {addr}:8765", flush=True)
        ok += 1
    if not ok:
        print("FATAL: tidak ada listen address yang berhasil", flush=True)
        return 2
    n = len(_load_keys())
    print(f"keys: {n} in {KEYS_FILE} | "
          f"legacy BRIDGE_TOKEN: {'on (/v1/*)' if TOKEN else 'off'} | "
          f"lease={LEASE_SECS}s wait={WAIT_SECS}s", flush=True)
    threading.Event().wait()


def main():
    ap = argparse.ArgumentParser(prog="bridge.py",
                                 description="Muse bridge v5 for 9Router")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the HTTP bridge (default)")
    g = sub.add_parser("keygen", help="create an API key (printed once)")
    g.add_argument("--role", required=True, choices=["user", "worker"])
    g.add_argument("--label", required=True)
    sub.add_parser("keylist", help="list keys (prefixes only)")
    d = sub.add_parser("keydel", help="revoke a key by prefix or label")
    d.add_argument("ident")
    args = ap.parse_args()
    if args.cmd == "keygen":
        return cmd_keygen(args.role, args.label)
    if args.cmd == "keylist":
        return cmd_keylist()
    if args.cmd == "keydel":
        return cmd_keydel(args.ident)
    return cmd_serve() or 0  # default: serve (v4-compatible: `python bridge.py`)


if __name__ == "__main__":
    sys.exit(main())
