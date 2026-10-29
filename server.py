#!/usr/bin/env python3
"""Local Claude Code session manager. Serves index.html + a small JSON API on 127.0.0.1."""
import json, os, re, signal, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kimi

PORT = int(os.environ.get("PORT", 7337))
HOME = Path.home()
CLAUDE = HOME / ".claude"
SESSIONS = CLAUDE / "sessions"
PROJECTS = CLAUDE / "projects"
HERE = Path(__file__).resolve().parent
RECENT_LIMIT = 80
SID_RE = re.compile(r"(session_)?[0-9a-f-]{36}")  # Claude ids are UUIDs, Kimi ids are session_<uuid>
# Extra flags for sessions started or resumed from the page, e.g. "--dangerously-skip-permissions".
CLAUDE_ARGS = os.environ.get("CSD_CLAUDE_ARGS", "").strip()
CLAUDE_CMD = "claude" + (" " + CLAUDE_ARGS if CLAUDE_ARGS else "")
STATE = HERE / "state.json"  # {"rest": {sid: {"since": ms, "until": ms|0}}, "archived": [sid]}


def load_state():
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("rest", {}); st.setdefault("archived", [])
    return st


def save_state(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def tidy_rest(st, live):
    """Wake sessions whose rest expired, or that went busy after resting (you used them)."""
    now = int(time.time() * 1000)
    by_id = {r["sessionId"]: r for r in live}
    changed = False
    for sid, r in list(st["rest"].items()):
        s = by_id.get(sid)
        expired = r.get("until") and r["until"] <= now
        used = s and s["status"] == "busy" and (s.get("statusUpdatedAt") or 0) > r.get("since", 0)
        if expired or used:
            del st["rest"][sid]; changed = True
    if changed:
        save_state(st)
    return st

_meta_cache = {}  # path -> (mtime, meta)
CWD_RE = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')


def transcript_meta(path):
    st = path.stat()
    hit = _meta_cache.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    meta = {"title": None, "lastPrompt": None, "cwd": None, "customTitle": None}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if raw.startswith(b'{"type":"ai-title"'):
                    try:
                        meta["title"] = json.loads(raw).get("aiTitle") or meta["title"]
                    except ValueError:
                        pass
                elif raw.startswith(b'{"type":"custom-title"'):
                    try:
                        meta["customTitle"] = json.loads(raw).get("customTitle") or meta["customTitle"]
                    except ValueError:
                        pass
                elif raw.startswith(b'{"type":"last-prompt"'):
                    try:
                        meta["lastPrompt"] = json.loads(raw).get("lastPrompt")
                    except ValueError:
                        pass
                elif meta["cwd"] is None and b'"cwd":"' in raw:
                    m = CWD_RE.search(raw.decode("utf-8", "replace"))
                    if m:
                        meta["cwd"] = json.loads('"' + m.group(1) + '"')
    except OSError:
        pass
    meta["title"] = meta["customTitle"] or meta["title"]
    _meta_cache[path] = (st.st_mtime, meta)
    return meta


_usage_cache = {"at": 0, "data": None}
USAGE_LOG = HERE / "usage_log.jsonl"  # weekly % over time, so we can tell how much of it was used today
DAILY_BUDGET = 100 / 7


def daily_budget(limits):
    """Today's share of the weekly limit: (weekly % now) - (weekly % at local midnight), against 100/7 per day."""
    wk = next((l for l in limits if l.get("kind") == "weekly_all"), None)
    if not wk:
        return None
    now = time.time()
    from datetime import datetime
    try:  # the API's reset time jitters by a second between calls; round it so one week = one key
        resets = datetime.fromtimestamp(round(datetime.fromisoformat(wk["resets_at"]).timestamp() / 600) * 600).isoformat()
    except (KeyError, TypeError, ValueError):
        resets = wk.get("resets_at")
    log = []
    try:
        log = [json.loads(l) for l in USAGE_LOG.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    last = log[-1] if log else None
    if not last or last.get("pct") != wk["percent"] or last.get("resets") != resets or now - last["t"] > 900:
        log.append({"t": now, "pct": wk["percent"], "resets": resets})
        log = [e for e in log if now - e["t"] < 10 * 86400]
        USAGE_LOG.write_text("".join(json.dumps(e) + "\n" for e in log))
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    def week_key(r):
        try:
            return round(datetime.fromisoformat(r).timestamp() / 600)
        except (TypeError, ValueError):
            return r
    same = [e for e in log if week_key(e.get("resets")) == week_key(resets)]  # entries from this weekly window
    before = [e for e in same if e["t"] <= midnight]
    window_start = None
    try:
        window_start = datetime.fromisoformat(resets).timestamp() - 7 * 86400
    except (TypeError, ValueError):
        pass
    if before:
        base, since, exact = before[-1]["pct"], midnight, True
    elif window_start and window_start >= midnight:
        base, since, exact = 0, window_start, True  # the week reset today
    else:
        first = next((e for e in same if e["t"] > midnight), None)
        base, since, exact = (first["pct"], first["t"], False) if first else (wk["percent"], now, False)
    # Rollover: every calendar day of the window adds 100/7. Whatever wasn't used before today carries over
    # (and overuse before today is paid back), so today's allowance = budget accrued through today - usage before today.
    if window_start:
        from datetime import date
        day_idx = (date.fromtimestamp(now) - date.fromtimestamp(window_start)).days + 1
    else:
        day_idx = 1
    accrued = min(100.0, day_idx * DAILY_BUDGET)
    allowance = accrued - base
    return {"budget": round(DAILY_BUDGET, 2), "allowance": round(allowance, 1), "carry": round(allowance - DAILY_BUDGET, 1),
            "used": max(0, wk["percent"] - base), "since": int(since * 1000), "exact": exact,
            "weekly": wk["percent"], "day": min(day_idx, 7)}


def plan_usage():
    """Plan limits (same numbers as /usage). The OAuth token stays on this machine and only goes to api.anthropic.com."""
    if time.time() - _usage_cache["at"] < 60 and _usage_cache["data"]:
        return _usage_cache["data"]
    import urllib.request
    raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True).stdout
    oauth = json.loads(raw or "{}").get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        return {"error": "Not signed in to Claude Code"}
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + oauth["accessToken"], "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "claude-session-deck"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
    except Exception as e:  # expired token, offline, etc.
        return {"error": "Usage unavailable (" + str(getattr(e, "code", "") or e)[:60] + "). Open Claude Code to refresh login."}
    ex = d.get("extra_usage") or {}
    data = {"plan": oauth.get("subscriptionType"), "limits": d.get("limits") or [],
            "extra": {"enabled": ex.get("is_enabled"), "used": ex.get("used_credits"), "limit": ex.get("monthly_limit")},
            "fetchedAt": int(time.time() * 1000)}
    data["daily"] = daily_budget(data["limits"])
    _usage_cache.update(at=time.time(), data=data)
    return data


_stats_cache = {"mtime": None, "data": None}


def prompt_stats():
    """Prompts per local day from ~/.claude/history.jsonl, for the contribution graph."""
    hist = CLAUDE / "history.jsonl"
    try:
        mt = hist.stat().st_mtime
    except OSError:
        return {"days": {}}
    if _stats_cache["mtime"] == mt:
        return _stats_cache["data"]
    days, projects_today = {}, {}
    today = time.strftime("%Y-%m-%d")
    with open(hist, "rb") as fh:
        for raw in fh:
            try:
                d = json.loads(raw)
                ts = d["timestamp"] / 1000
            except (ValueError, KeyError, TypeError):
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            days[day] = days.get(day, 0) + 1
            if day == today:
                proj = os.path.basename(d.get("project") or "") or "~"
                projects_today[proj] = projects_today.get(proj, 0) + 1
    data = {"days": days, "projectsToday": projects_today}
    _stats_cache.update(mtime=mt, data=data)
    return data


_tail_cache = {}  # path -> (mtime, messages)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def thread(path, n=12):
    """Read a growing tail until it holds enough chat messages (big tool outputs can crowd them out)."""
    st = path.stat()
    hit = _tail_cache.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1][-n:]
    for chunk in (600_000, 2_500_000, 8_000_000):
        _tail_cache.pop(path, None)
        msgs = _thread_chunk(path, n, chunk)
        if len(_tail_cache[path][1]) >= 30 or chunk >= st.st_size:
            break
    return msgs


def _thread_chunk(path, n, chunk):
    """Last n human prompts / assistant replies from a transcript (reads only the file tail)."""
    st = path.stat()
    hit = _tail_cache.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1][-n:]
    with open(path, "rb") as fh:
        fh.seek(max(0, st.st_size - chunk))
        lines = fh.read().split(b"\n")
    if st.st_size > chunk:
        lines = lines[1:]  # first line is probably cut
    msgs, ctx = [], {"model": None, "tokens": None}
    for raw in lines:
        if not (raw.startswith(b'{"parentUuid"') or b'"type":"assistant"' in raw[:400] or b'"type":"user"' in raw[:400]
                or b'"compact_boundary"' in raw[:600]):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        kind = d.get("type")
        if d.get("subtype") == "compact_boundary":
            meta = d.get("compactMetadata") or {}
            if meta.get("postTokens"):
                ctx = {**ctx, "tokens": meta["postTokens"]}  # stays until the next reply reports real usage
            msgs.append({"role": "system", "text": f"Compacted: {round((meta.get('preTokens') or 0) / 1000)}k → {round((meta.get('postTokens') or 0) / 1000)}k tokens",
                         "ts": d.get("timestamp")})
            continue
        if d.get("isCompactSummary"):
            continue
        if kind not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        if kind == "assistant":
            m = d.get("message") or {}
            u = m.get("usage") or {}
            if u and m.get("model") and m.get("model") != "<synthetic>":
                ctx = {"model": m["model"], "tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                       + (u.get("cache_creation_input_tokens") or 0) + (u.get("output_tokens") or 0)}
        text = _text_of((d.get("message") or {}).get("content")).strip()
        if not text:
            continue
        if kind == "user":
            if d.get("toolUseResult") is not None or text.startswith(("<command-", "<local-command", "<system-reminder", "[Request interrupted", "Caveat:")):
                continue
            msgs.append({"role": "user", "text": text, "ts": d.get("timestamp")})
        else:
            if msgs and msgs[-1]["role"] == "assistant":
                msgs[-1]["text"] += "\n\n" + text
                msgs[-1]["final"] = text
                msgs[-1]["ts"] = d.get("timestamp")
            else:
                msgs.append({"role": "assistant", "text": text, "final": text, "ts": d.get("timestamp")})
    _tail_cache[path] = (st.st_mtime, msgs, ctx)
    return msgs[-n:]


def context_info(path):
    thread(path)
    return _tail_cache[path][2]


def ps_info():
    out = subprocess.run(["ps", "-axo", "pid=,etime=,%cpu=,rss=,tty=,command="],
                         capture_output=True, text=True).stdout
    info = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, etime, cpu, rss, tty, cmd = parts
        info[int(pid)] = {"etime": etime, "cpu": float(cpu), "rssMB": round(int(rss) / 1024),
                          "tty": tty, "command": cmd}
    return info


def find_transcript(session_id):
    for p in PROJECTS.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def live_sessions(ps):
    rows = []
    for f in SESSIONS.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        p = ps.get(pid)
        if not p or "claude" not in p["command"]:
            continue  # stale registry entry
        t = find_transcript(d.get("sessionId", ""))
        meta = transcript_meta(t) if t else {}
        last = next((m for m in reversed(thread(t)) if m["role"] == "assistant"), None) if t else None
        rows.append({
            "pid": pid, "sessionId": d.get("sessionId"), "name": d.get("name"),
            "cwd": d.get("cwd"), "status": d.get("status", "unknown"),
            "version": d.get("version"), "startedAt": d.get("startedAt"),
            "statusUpdatedAt": d.get("statusUpdatedAt") or d.get("updatedAt"),
            "bridgeSessionId": d.get("bridgeSessionId"),
            "title": meta.get("title"), "lastPrompt": meta.get("lastPrompt"),
            "lastResult": last["final"][:900] if last else None,
            **({"model": None, "tokens": None} if not t else {"model": context_info(t)["model"], "ctxTokens": context_info(t)["tokens"]}),
            "tty": p["tty"], "cpu": p["cpu"], "rssMB": p["rssMB"], "etime": p["etime"],
            "flags": p["command"].split(None, 1)[1] if " " in p["command"] else "",
        })
    rows.sort(key=lambda r: r.get("statusUpdatedAt") or 0, reverse=True)
    return rows


def recent_sessions(live_ids):
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for p in files[:RECENT_LIMIT]:
        sid = p.stem
        meta = transcript_meta(p)
        st = p.stat()
        rows.append({
            "sessionId": sid, "title": meta["title"], "lastPrompt": meta["lastPrompt"],
            "cwd": meta["cwd"] or p.parent.name, "modifiedAt": int(st.st_mtime * 1000),
            "sizeKB": round(st.st_size / 1024), "live": sid in live_ids,
        })
    return rows


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def as_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def iterm_focus(tty):
    dev = "/dev/" + tty
    return osa(f'''
tell application "iTerm"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is {as_str(dev)} then
          select w
          tell t to select
          tell s to select
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "notfound"''')


def iterm_run(cmd):
    return osa(f'''
tell application "iTerm"
  activate
  if (count of windows) = 0 then
    create window with default profile
    tell current session of current window to write text {as_str(cmd)}
  else
    tell current window
      set newTab to (create tab with default profile)
      tell current session of newTab to write text {as_str(cmd)}
    end tell
  end if
end tell
return "ok"''')


def iterm_type(tty, text):
    """Type a line into the iTerm session attached to tty (used for /rename)."""
    return osa(f'''
tell application "iTerm"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is {as_str("/dev/" + tty)} then
          tell s to write text {as_str(text)}
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "notfound"''')


def iterm_send(tty, text):
    """Paste text into the iTerm session on tty (bracketed paste keeps newlines), then press Enter."""
    pasted = "\x1b[200~" + text + "\x1b[201~" if "\n" in text else text
    return osa(f'''
tell application "iTerm"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is {as_str("/dev/" + tty)} then
          tell s to write text {as_str(pasted)} newline no
          delay 0.15
          tell s to write text ""
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "notfound"''')


def recent_prompts(session_id, n=8):
    out = []
    try:
        with open(CLAUDE / "history.jsonl", "rb") as fh:
            for raw in fh:
                if session_id.encode() in raw:
                    try:
                        out.append(json.loads(raw).get("display", "")[:300])
                    except ValueError:
                        pass
    except OSError:
        pass
    return out[-n:]


def quick_names(title, cwd, prompts):
    proj = os.path.basename(cwd or "") or "home"
    base = (title or (prompts[-1] if prompts else "") or "session").strip()
    words = re.findall(r"[A-Za-z0-9]+", base)[:5]
    out = []
    if title:
        out.append(f"{proj}: {title}")
    if words:
        out.append(" ".join(w.capitalize() if w.islower() else w for w in words))
        out.append(proj + "-" + "-".join(w.lower() for w in words[:3]))
    seen, uniq = set(), []
    for x in out:
        if x.lower() not in seen and x.lower() != (title or "").lower():
            seen.add(x.lower()); uniq.append(x[:60])
    return uniq


def ai_names(title, cwd, prompts):
    ask = ("Suggest 4 short, specific names (max 6 words) for a Claude Code session so the user can find it "
           "later in /resume. Mix languages only if the prompts do. Folder: " + os.path.basename(cwd or "") +
           ". Current title: " + (title or "none") + ". Recent user prompts:\n- " + "\n- ".join(prompts[-6:]) +
           "\nOutput ONLY the 4 names, one per line, no numbering or quotes.")
    r = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence", ask],
                       capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL, cwd=str(HERE))
    names = [re.sub(r'^[\s\-*\d.)"]+|["\s]+$', "", l) for l in r.stdout.splitlines()]
    return [x[:60] for x in names if 2 < len(x) <= 80][:4]


def registered_claude_pid(pid, ps):
    f = SESSIONS / f"{pid}.json"
    return f.exists() and pid in ps and "claude" in ps[pid]["command"]


def agent_pid(pid, ps):
    """A running Claude Code or Kimi Code process we may focus or quit."""
    return registered_claude_pid(pid, ps) or (pid in ps and ps[pid]["command"].split()[0].endswith("kimi-code"))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/sessions":
            ps = ps_info()
            live = live_sessions(ps) + kimi.live_sessions(ps)
            ids = {r["sessionId"] for r in live}
            st = tidy_rest(load_state(), live)
            self._json(200, {"now": int(time.time() * 1000), "home": str(HOME), "live": live,
                             "recent": sorted(recent_sessions(ids) + kimi.recent_sessions(ids), key=lambda r: r["modifiedAt"], reverse=True)[:RECENT_LIMIT], "rest": st["rest"],
                             "archived": st["archived"]})
        elif self.path.startswith("/api/thread?id="):
            sid = self.path.split("=", 1)[1]
            if kimi.is_kimi_id(sid):
                msgs = kimi.thread(sid)
                return self._json(200, {"messages": msgs}) if msgs is not None else self._json(404, {"error": "Session not found"})
            t = find_transcript(sid) if SID_RE.fullmatch(sid) else None
            if not t:
                return self._json(404, {"error": "Transcript not found"})
            self._json(200, {"messages": thread(t, 30)})
        elif self.path == "/api/usage":
            self._json(200, plan_usage())
        elif self.path == "/api/stats":
            self._json(200, prompt_stats())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        # Block cross-site requests: only our own page sends this header + same-origin.
        origin = self.headers.get("Origin", "")
        if self.headers.get("X-CSM") != "1" or (origin and not origin.startswith(f"http://127.0.0.1:{PORT}")
                                                and not origin.startswith(f"http://localhost:{PORT}")):
            return self._json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            ps = ps_info()
            if self.path == "/api/focus":
                pid = int(data["pid"])
                if not agent_pid(pid, ps):
                    return self._json(400, {"error": "Not a running Claude session"})
                res = iterm_focus(ps[pid]["tty"])
                if res != "ok":
                    return self._json(404, {"error": f"No iTerm tab found on {ps[pid]['tty']}"})
                return self._json(200, {"ok": True})
            if self.path == "/api/kill":
                pid = int(data["pid"])
                if not agent_pid(pid, ps):
                    return self._json(400, {"error": "Not a running Claude session"})
                os.kill(pid, signal.SIGTERM if not data.get("force") else signal.SIGKILL)
                return self._json(200, {"ok": True})
            if self.path == "/api/resume":
                sid = data["sessionId"]
                if not SID_RE.fullmatch(sid):
                    return self._json(400, {"error": "Bad session id"})
                cwd = data.get("cwd") or str(HOME)
                if not os.path.isdir(cwd):
                    return self._json(400, {"error": f"Folder no longer exists: {cwd}"})
                iterm_run(f"cd {shq(cwd)} && " + (f"kimi -S {sid}" if kimi.is_kimi_id(sid) else f"{CLAUDE_CMD} --resume {sid}"))
                return self._json(200, {"ok": True})
            if self.path in ("/api/suggest", "/api/rename"):
                sid = data["sessionId"]
                if kimi.is_kimi_id(sid):
                    return self._json(400, {"error": "Rename Kimi sessions inside Kimi"})
                if not SID_RE.fullmatch(sid):
                    return self._json(400, {"error": "Bad session id"})
                t = find_transcript(sid)
                meta = transcript_meta(t) if t else {}
                if self.path == "/api/suggest":
                    prompts = recent_prompts(sid)
                    if data.get("ai"):
                        return self._json(200, {"names": ai_names(meta.get("title"), meta.get("cwd"), prompts)})
                    return self._json(200, {"names": quick_names(meta.get("title"), meta.get("cwd"), prompts)})
                name = " ".join(str(data.get("name", "")).split())[:80]
                if not name:
                    return self._json(400, {"error": "Name can't be empty"})
                live = next((json.loads(f.read_text()) for f in SESSIONS.glob("*.json")
                             if sid in f.read_text()), None)
                if live and live.get("pid") in ps and "claude" in ps[live["pid"]]["command"]:
                    if live.get("status") == "busy":
                        return self._json(409, {"error": "Session is busy. Rename it when it's idle."})
                    if iterm_type(ps[live["pid"]]["tty"], "/rename " + name) != "ok":
                        return self._json(404, {"error": "Couldn't find its iTerm tab"})
                    return self._json(200, {"ok": True, "via": "live"})
                if not t:
                    return self._json(404, {"error": "Transcript not found"})
                line = {"type": "custom-title", "customTitle": name, "sessionId": sid,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
                with open(t, "a") as fh:
                    fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                return self._json(200, {"ok": True, "via": "file"})
            if self.path in ("/api/rest", "/api/archive"):
                sid = data["sessionId"]
                if not SID_RE.fullmatch(sid):
                    return self._json(400, {"error": "Bad session id"})
                st = load_state()
                if self.path == "/api/rest":
                    if data.get("wake"):
                        st["rest"].pop(sid, None)
                    else:
                        st["rest"][sid] = {"since": int(time.time() * 1000), "until": int(data.get("until") or 0)}
                else:
                    st["archived"] = [x for x in st["archived"] if x != sid]
                    if data.get("archived"):
                        st["archived"].append(sid)
                        st["rest"].pop(sid, None)
                save_state(st)
                return self._json(200, {"ok": True})
            if self.path == "/api/cmd":
                pid = int(data["pid"])
                cmd = str(data.get("cmd", "")).strip()
                ok = re.fullmatch(r"/(compact|clear|fast|model (opus|sonnet|haiku|fable|default|opus\[1m\]|sonnet\[1m\])|effort (low|medium|high|xhigh|max|auto))", cmd)
                if not ok:
                    return self._json(400, {"error": "Command not allowed"})
                if not registered_claude_pid(pid, ps):
                    return self._json(400, {"error": "Not a running Claude session"})
                reg = json.loads((SESSIONS / f"{pid}.json").read_text())
                if reg.get("status") == "busy":
                    return self._json(409, {"error": "It's busy. Try again when it's idle."})
                if iterm_send(ps[pid]["tty"], cmd) != "ok":
                    return self._json(404, {"error": "Couldn't find its iTerm tab"})
                return self._json(200, {"ok": True})
            if self.path == "/api/reply":
                sid = data["sessionId"]
                text = str(data.get("text", "")).strip()
                if not SID_RE.fullmatch(sid) or not text:
                    return self._json(400, {"error": "Write something first"})
                live = None
                for f in SESSIONS.glob("*.json"):
                    try:
                        d = json.loads(f.read_text())
                    except (OSError, ValueError):
                        continue
                    if d.get("sessionId") == sid and d.get("pid") in ps and "claude" in ps[d["pid"]]["command"]:
                        live = d
                if not live and kimi.is_kimi_id(sid):
                    k = next((r for r in kimi.live_sessions(ps) if r["sessionId"] == sid), None)
                    if not k:
                        return self._json(409, {"error": "This Kimi session isn't open. Resume it first."})
                    live = {"pid": k["pid"], "status": k["status"]}
                if live:
                    if iterm_send(ps[live["pid"]]["tty"], text) != "ok":
                        return self._json(404, {"error": "Couldn't find its iTerm tab"})
                    st = load_state()
                    if st["rest"].pop(sid, None) is not None:
                        save_state(st)
                    return self._json(200, {"ok": True, "via": "live", "queued": live.get("status") == "busy"})
                t = find_transcript(sid)
                cwd = (transcript_meta(t).get("cwd") if t else None) or str(HOME)
                if not os.path.isdir(cwd):
                    return self._json(400, {"error": f"Folder no longer exists: {cwd}"})
                iterm_run(f"cd {shq(cwd)} && {CLAUDE_CMD} --resume {sid} {shq(text)}")
                return self._json(200, {"ok": True, "via": "resume"})
            if self.path == "/api/new":
                cwd = os.path.expanduser(data.get("cwd") or "~")
                if not os.path.isdir(cwd):
                    return self._json(400, {"error": f"Folder not found: {cwd}"})
                iterm_run(f"cd {shq(cwd)} && {CLAUDE_CMD}")
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except (KeyError, ValueError) as e:
            return self._json(400, {"error": f"Bad request: {e}"})
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "Claude took too long to suggest names"})
        except (RuntimeError, OSError) as e:
            return self._json(500, {"error": str(e)})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"Claude session manager on http://127.0.0.1:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
