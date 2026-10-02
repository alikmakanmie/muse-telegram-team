#!/usr/bin/env python3
"""Persistent worker poll loop untuk muse-bridge (cron muse-bridge-worker).

- Baca worker key SEKALI dari KEYFILE (tidak pernah dicetak).
- Loop sampai ~540 detik: GET /muse/pending?limit=3 (claim atomik di server).
- Tiap job: ACK instan mekanis POST /muse/answer {"id": jid, "done": False}
  (done:false, TANPA teks apapun), tulis job ke JOBDIR, jalankan fast-answer.py.
- FAST_EXIT 0 = selesai (fast-answer sudah post jawaban final).
- NEEDS_MANUAL <jid> = exit != 0 (exit 2 tidak eligible / exit 1 gagal):
  job JSON tersimpan di JOBDIR/<jid>.json untuk dijawab manual agen.
- Exit 3 pada 401 (lapor ke chat SEKALI), 4 jika bridge tak terjangkau.
- Idle total: DIAM, tidak melapor.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BRIDGE = "http://127.0.0.1:8765"
KEYFILE = os.path.expanduser("~/workspace/muse-bridge/secrets/bridge-worker.key")
FAST_ANSWER = os.path.expanduser("~/workspace/muse-bridge/fast-answer.py")
JOBDIR = "/tmp/muse-worker-jobs"
RUN_SECS = 540
POLL_TIMEOUT = 5


def log(s):
    try:
        print(s, flush=True)
    except Exception:
        # stdout locale sempit (mis. ASCII di cron): jangan sampai logging
        # membunuh loop — tulis versi aman-ASCII.
        print(str(s).encode("ascii", "replace").decode("ascii"), flush=True)


def _sanitize(o):
    """Ganti lone surrogate / karakter tak-ter-encode dengan '?' secara rekursif."""
    if isinstance(o, str):
        return o.encode("utf-8", "replace").decode("utf-8")
    if isinstance(o, dict):
        return {_sanitize(k): _sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sanitize(v) for v in o]
    return o


def _req(method, path, key, body=None, timeout=10):
    r = urllib.request.Request(
        BRIDGE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace") or "{}"
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw


def main():
    os.makedirs(JOBDIR, exist_ok=True)
    with open(KEYFILE) as f:
        key = f.read().strip()
    t_end = time.time() + RUN_SECS
    manual_left = []
    while time.time() < t_end:
        try:
            status, jobs = _req("GET", "/muse/pending?limit=3", key,
                                timeout=POLL_TIMEOUT)
        except urllib.error.HTTPError as e:
            log("BRIDGE_HTTP_%d" % e.code)
            if e.code == 401:
                log("AUTH_FAILED_EXIT")
                return 3
            time.sleep(5)
            continue
        except Exception as e:
            log("BRIDGE_UNREACHABLE %s" % type(e).__name__)
            return 4
        if not isinstance(jobs, dict):
            time.sleep(1)
            continue
        jobs = jobs.get("jobs") or []
        if not jobs:
            time.sleep(1)
            continue
        for job in jobs:
            jid = job.get("id", "?")
            try:
                _req("POST", "/muse/answer", key,
                     {"id": jid, "done": False}, timeout=8)
            except Exception as e:
                log("ACK_FAIL %s %s" % (jid, type(e).__name__))
                continue
            jf = os.path.join(JOBDIR, jid + ".json")
            try:
                with open(jf, "w", encoding="utf-8") as f:
                    json.dump(job, f, ensure_ascii=True)
            except Exception:
                # Job mengandung karakter yang tidak lolos encode (mis. lone
                # surrogate dari teks Telegram): sanitasi lalu tulis ulang.
                # Gagal total pun TIDAK boleh membunuh loop — release job
                # kembali ke antrean dan lanjut polling.
                try:
                    with open(jf, "w", encoding="utf-8") as f:
                        json.dump(_sanitize(job), f, ensure_ascii=True)
                except Exception as e:
                    log("JOB_DUMP_FAIL %s %s" % (jid, type(e).__name__))
                    try:
                        _req("POST", "/muse/release", key, {"id": jid},
                             timeout=8)
                        log("RELEASED %s" % jid)
                    except Exception as e2:
                        log("RELEASE_FAIL %s %s" % (jid, type(e2).__name__))
                    continue
            n_tools = len((job.get("request") or {}).get("tools") or [])
            log("JOB %s tools=%d" % (jid, n_tools))
            try:
                p = subprocess.run([sys.executable, FAST_ANSWER, jf],
                                   capture_output=True, text=True,
                                   errors="replace", timeout=300)
                log("FAST_EXIT %s %d %s %s" % (
                    jid, p.returncode,
                    (p.stdout or "").strip()[:120],
                    (p.stderr or "").strip()[:120]))
            except Exception as e:
                log("FAST_ERROR %s %s" % (jid, type(e).__name__))
                p = None
            if p is None or p.returncode != 0:
                manual_left.append(jid)
                log("NEEDS_MANUAL %s" % jid)
                # OPTIMASI LATENSI: keluar loop SEGERA saat ada job manual
                # (mis. karyawan) supaya agen yang menunggu script ini
                # langsung terbangun & menjawab, bukan terblokir s/d 540 dtk.
                log("MANUAL_LEFT %s" % ",".join(manual_left))
                return 0
    log("LOOP_DONE")
    if manual_left:
        log("MANUAL_LEFT %s" % ",".join(manual_left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
