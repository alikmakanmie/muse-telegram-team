#!/usr/bin/env python3
"""Fast-path responder untuk Muse bridge (2 Okt 2026, permintaan user: kecepatan).

Dipanggil SEGERA setelah worker claim+ACK sebuah job. Menjawab chat sederhana
lewat SATU panggilan HTTP langsung ke Bariska API — tanpa penalaran agen yang
panjang. Ini BUKAN template/auto-reply: jawabannya tetap dihasilkan model AI
(Bariska), hanya jalurnya yang pendek.

PENTING — anti-loop: request dikirim LANGSUNG ke https://api.bariska.cloud/v1
(memakai key Bariska), BUKAN lewat 9Router. Kalau lewat 9Router dengan combo
'bariska' -> ["bariska/auto", "dhodi/dhodi"], saat Bariska down 9Router akan
fallback ke dhodi/dhodi = bridge ini sendiri -> loop antrean tak berujung.
Jalur langsung tidak punya fallback, jadi loop mustahil terjadi.

Pemakaian:
    python3 fast-answer.py <job-json-file>      # atau '-' untuk baca dari stdin
    echo '<job-json>' | python3 fast-answer.py -

Exit codes:
    0 = job BERHASIL dijawab & final dipost ke bridge (worker lanjut loop)
    2 = TIDAK eligible (ada tools / butuh penalaran agen) -> jawab manual
    1 = GAGAL (Bariska error/timeout) -> jawab manual via slow path

Key tidak pernah dicetak ke output.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HOME = os.path.expanduser("~")
BRIDGE = "http://127.0.0.1:8765"
WORKER_KEY_FILE = os.path.join(HOME, "workspace/muse-bridge/secrets/bridge-worker.key")
BARISKA_KEY_FILE = os.path.join(HOME, "workspace/bariska/secrets/api_key.txt")
BARISKA_URL = "https://api.bariska.cloud/v1/chat/completions"
BARISKA_MODEL = "auto"          # upstream model id di balik combo 'bariska'
LLM_TIMEOUT = 90                # detik; satu panggilan cepat
MAX_TOKENS_CAP = 1000

DEFAULT_PERSONA = (
    "Kamu Dhodi, asisten pribadi PEMILIK: hangat, membantu, dan sedikit playful. "
    "Jawab dalam bahasa yang dipakai user (default Bahasa Indonesia). "
    "Singkat, langsung ke inti, tanpa basa-basi. Jangan pernah menyebut dirimu "
    "sebagai AI generik — kamu Dhodi."
)


def _load_key(path):
    with open(path) as f:
        return f.read().strip()


def _bridge_post_answer(worker_key, jid, content):
    payload = {"id": jid, "content": content, "done": True}
    r = urllib.request.Request(
        BRIDGE + "/muse/answer",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + worker_key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status


def main():
    t0 = time.time()
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    try:
        if src == "-":
            job = json.load(sys.stdin)
        else:
            with open(src, encoding="utf-8") as f:
                job = json.load(f)
    except Exception as e:
        print("FAST_FAIL bad-job-json", type(e).__name__, flush=True)
        return 1

    jid = job.get("id", "")
    req = job.get("request") or {}

    # ---- eligibility: hanya chat sederhana ----
    if req.get("tools"):
        print("FAST_SKIP tools-present", flush=True)
        return 2
    msgs = req.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        print("FAST_SKIP no-messages", flush=True)
        return 2
    # Job karyawan AgentX (persona di system message) WAJIB dijawab manual
    # oleh worker (native Dhodi), bukan via fast path Bariska.
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            sys_txt = str(m.get("content", "")).lower()
            if "karyawan" in sys_txt or any(
                n in sys_txt for n in
                ("bimo", "sinta", "raka", "nadia", "dimas",
                 "fajar", "galih", "kirana", "laras", "yoga", "wulan")
            ):
                print("FAST_SKIP employee-persona", flush=True)
                return 2
            break
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "tool":
            print("FAST_SKIP tool-history", flush=True)
            return 2
    last = msgs[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        print("FAST_SKIP last-not-user", flush=True)
        return 2
    if not str(last.get("content", "")).strip():
        print("FAST_SKIP empty-last", flush=True)
        return 2

    # ---- bangun messages minimal: persona + 10 pesan terakhir ----
    system = [m for m in msgs
              if isinstance(m, dict) and m.get("role") == "system"][:1]
    rest = [m for m in msgs
            if not (isinstance(m, dict) and m.get("role") == "system")][-10:]
    clean = []
    for m in rest:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content", "")
        c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        if c.strip():
            clean.append({"role": role, "content": c})
    if not clean:
        print("FAST_SKIP no-clean-messages", flush=True)
        return 2
    if system and str(system[0].get("content", "")).strip():
        out_msgs = system
    else:
        out_msgs = [{"role": "system", "content": DEFAULT_PERSONA}]
    out_msgs = out_msgs + clean

    max_tokens = req.get("max_tokens") or 400
    try:
        max_tokens = max(50, min(int(max_tokens), MAX_TOKENS_CAP))
    except Exception:
        max_tokens = 400

    try:
        bariska_key = _load_key(BARISKA_KEY_FILE)
        worker_key = _load_key(WORKER_KEY_FILE)
    except Exception as e:
        print("FAST_FAIL key-read", type(e).__name__, flush=True)
        return 1
    if not bariska_key or not worker_key:
        print("FAST_FAIL empty-key", flush=True)
        return 1

    # ---- SATU panggilan langsung ke Bariska (tanpa 9Router) ----
    payload = {"model": BARISKA_MODEL, "messages": out_msgs,
               "max_tokens": max_tokens, "stream": False}
    # Cloudflare di api.bariska.cloud menolak User-Agent default urllib (403/1010);
    # User-Agent browser diperlukan agar request lolos.
    r = urllib.request.Request(
        BARISKA_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + bariska_key,
                 "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(r, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            eb = e.read().decode(errors="replace")[:300]
        except Exception:
            eb = ""
        print("FAST_FAIL bariska-http", e.code, eb.replace(bariska_key, "***"),
              flush=True)
        return 1
    except Exception as e:
        print("FAST_FAIL bariska-conn", type(e).__name__, flush=True)
        return 1

    try:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        text = text.strip()
    except Exception:
        text = ""
    if not text:
        print("FAST_FAIL empty-answer", flush=True)
        return 1

    # ---- post final ke bridge ----
    try:
        st = _bridge_post_answer(worker_key, jid, text)
    except Exception as e:
        print("FAST_FAIL bridge-post", type(e).__name__, flush=True)
        return 1
    if st != 200:
        print("FAST_FAIL bridge-post-http", st, flush=True)
        return 1

    print("FAST_OK %.1fs %dchars" % (time.time() - t0, len(text)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
