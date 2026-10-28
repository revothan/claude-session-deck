"""Kimi Code (Moonshot) sessions: read ~/.kimi-code so they show up next to Claude Code ones."""
import json, os, subprocess, time
from pathlib import Path

KIMI = Path.home() / ".kimi-code"
SESS = KIMI / "sessions"
CTX_MAX = 262_144  # Kimi K-series context window

_wire_cache = {}  # wire path -> (mtime, parsed)


def is_kimi_id(sid):
    return isinstance(sid, str) and sid.startswith("session_")


def session_dir(sid):
    if not is_kimi_id(sid) or "/" in sid:
        return None
    for d in SESS.glob(f"*/{sid}"):
        return d
    return None


def _state(d):
    try:
        return json.loads((d / "state.json").read_text())
    except (OSError, ValueError):
        return None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_wire(d, tail=3_000_000):
    """Messages, status, model and context size from agents/main/wire.jsonl."""
    wire = d / "agents" / "main" / "wire.jsonl"
    try:
        st = wire.stat()
    except OSError:
        return {"messages": [], "status": "idle", "statusAt": None, "model": None, "tokens": None}
    hit = _wire_cache.get(wire)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    with open(wire, "rb") as fh:
        fh.seek(max(0, st.st_size - tail))
        lines = fh.read().split(b"\n")
    if st.st_size > tail:
        lines = lines[1:]
    msgs, status, status_at, model, tokens, pending = [], "idle", None, None, None, set()
    for raw in lines:
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        t, at = o.get("type"), o.get("time")
        if t == "agent.turn.started":
            status, status_at = "busy", at
        elif t in ("agent.turn.ended", "turn.ended"):
            status, status_at = "idle", at
            pending.clear()
        elif t == "interaction.request":
            pending.add(o.get("id")); status, status_at = "waiting", at
        elif t == "interaction.resolved":
            pending.discard(o.get("id"))
            if not pending:
                status, status_at = "busy", at
        elif t == "llm.request":
            model = o.get("modelAlias") or o.get("model") or model
        elif t == "token_counting.measured":
            tokens = o.get("tokens") or tokens
        elif t == "turn.step.interrupted" and o.get("reason") == "error":
            msgs.append({"role": "system", "text": (o.get("message") or "Error")[:300],
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at / 1000)) if at else None})
        elif t == "agent.message.appended":
            m = (o.get("message") or {}).get("message") or {}
            meta = (o.get("message") or {}).get("meta") or {}
            text = _text(m.get("content")).strip()
            if not text:
                continue
            ts = meta.get("createdAt")
            if m.get("role") == "user" and (meta.get("origin") or {}).get("kind") == "user":
                msgs.append({"role": "user", "text": text, "ts": ts})
            elif m.get("role") == "assistant":
                if msgs and msgs[-1]["role"] == "assistant":
                    msgs[-1]["text"] += "\n\n" + text
                    msgs[-1]["final"] = text
                    msgs[-1]["ts"] = ts or msgs[-1]["ts"]
                else:
                    msgs.append({"role": "assistant", "text": text, "final": text, "ts": ts})
    data = {"messages": msgs, "status": status, "statusAt": status_at, "model": model, "tokens": tokens}
    _wire_cache[wire] = (st.st_mtime, data)
    return data


_titles = {"at": 0, "map": {}}


def _iterm_titles():
    if time.time() - _titles["at"] < 8:
        return _titles["map"]
    _titles.update(at=time.time(), map=_read_titles())
    return _titles["map"]


def _read_titles():
    """tty -> iTerm tab title. Kimi puts its session title there, which tells us which session a process has open."""
    script = '''tell application "iTerm"
  set out to ""
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set out to out & (tty of s) & (character id 9) & (name of s) & linefeed
      end repeat
    end repeat
  end repeat
  return out
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            tty, name = line.split("\t", 1)
            out[tty.replace("/dev/", "")] = name.replace("(kimi-code)", "").strip()
    return out


def _cwd_of(pid):
    r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True)
    return next((l[1:] for l in r.stdout.splitlines() if l.startswith("n")), None)


def all_sessions():
    out = []
    for d in SESS.glob("*/session_*"):
        s = _state(d)
        if s and not s.get("archived"):
            out.append((d, s))
    return out


def live_sessions(ps):
    pids = [p for p, i in ps.items() if i["command"].split()[0].endswith("kimi-code")]
    if not pids:
        return []
    titles = _iterm_titles()
    sessions = all_sessions()
    taken, rows = set(), []
    for pid in sorted(pids):
        p = ps[pid]
        cwd = _cwd_of(pid)
        cands = [(d, s) for d, s in sessions if s.get("cwd") == cwd and s["id"] not in taken]
        title = titles.get(p["tty"], "")
        # The tab title is the session title, cut short; prefer that match, else the most recently used session.
        title = title.lstrip("✳⠂⠐·*• ").strip()
        match = [c for c in cands if title and (c[1].get("title") or "").startswith(title[:28])]
        pick = max(match or cands, key=lambda c: c[1].get("updatedAt") or 0, default=None)
        if not pick:
            continue
        d, s = pick
        taken.add(s["id"])
        w = parse_wire(d)
        last = next((m for m in reversed(w["messages"]) if m["role"] == "assistant"), None)
        rows.append({
            "agent": "kimi", "pid": pid, "sessionId": s["id"],
            "name": "kimi · " + os.path.basename(cwd or ""), "cwd": cwd,
            "status": "busy" if w["status"] == "busy" else ("waiting" if w["status"] == "waiting" else "idle"),
            "version": "kimi", "startedAt": s.get("createdAt"),
            "statusUpdatedAt": w["statusAt"] or s.get("updatedAt"),
            "bridgeSessionId": None, "title": s.get("title"), "lastPrompt": s.get("lastPrompt"),
            "lastResult": last["final"][:900] if last else None,
            "tty": p["tty"], "cpu": p["cpu"], "rssMB": p["rssMB"], "etime": p["etime"], "flags": "",
            "model": w["model"], "ctxTokens": w["tokens"], "ctxMax": CTX_MAX,
        })
    return rows


def recent_sessions(live_ids):
    rows = []
    for d, s in all_sessions():
        wire = d / "agents" / "main" / "wire.jsonl"
        try:
            size = wire.stat().st_size
        except OSError:
            size = 0
        rows.append({"agent": "kimi", "sessionId": s["id"], "title": s.get("title"), "lastPrompt": s.get("lastPrompt"),
                     "cwd": s.get("cwd"), "modifiedAt": s.get("updatedAt") or 0, "sizeKB": round(size / 1024),
                     "live": s["id"] in live_ids})
    return rows


def thread(sid, n=30):
    d = session_dir(sid)
    return parse_wire(d)["messages"][-n:] if d else None


def cwd_of_session(sid):
    d = session_dir(sid)
    s = _state(d) if d else None
    return s.get("cwd") if s else None
