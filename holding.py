#!/usr/bin/env python3
"""Print which Claude Code agents still hold background work although Herdr shows them done or idle.

Output: {"<terminal address>": "<why>"}, e.g. {"t17": "schedule 17 9,13,17 * * 1-5", "t2m": "1 monitor"}.
Other machines (hc machine ls) run this same script over `hc exec --on`; their rows come back as <machine>/<terminal>.
The cases and their sources: README.md.

Three sources, all Claude Code internals:
  session   <config dir>/sessions/<pid>.json ($CLAUDE_CONFIG_DIR and every ~/.claude* that has one): status "shell" (background shell or shell monitor)
            or "busy" after the turn ended (background agents, workflows, teammates)
  footer    the "N monitor · N shell · ↓ to manage" line on the screen: says which, and is the only
            place a monitor that runs no shell could show
  transcript  schedules, wake-ups and PR subscriptions, which live in Claude's memory only
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

CONFIG_DIRS = sorted({os.path.dirname(d) for d in glob.glob(os.path.expanduser("~/.claude*/sessions"))}
                     | ({os.environ["CLAUDE_CONFIG_DIR"]} if os.environ.get("CLAUDE_CONFIG_DIR") else set()))
CACHE = os.path.expanduser("~/.cache/herdr-agents-holding.json")
CRON_EXPIRY_DAYS = 7  # Claude's own limit for recurring session schedules
MARKS = ('"humanSchedule"', '"CronDelete"', "scheduled_task_fire", "session_cron_carry",
         '"ScheduleWakeup"', "ubscribePR", "subscribe_pr_activity")


def hc(*args):
    r = subprocess.run(["hc", *args], capture_output=True, text=True, timeout=20)
    return r.stdout if r.returncode == 0 else ""


def ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def sessions():
    """pid -> (session record, config dir), for every Claude session file."""
    out = {}
    for d in CONFIG_DIRS:
        for f in glob.glob(f"{d}/sessions/*.json"):
            try:
                s = json.load(open(f))
                out[s["pid"]] = (s, d)
            except (OSError, ValueError, KeyError):
                pass
    return out


def footer(address):
    """Claude's own counts of what is running, e.g. ["1 monitor", "2 shells"]."""
    for line in reversed(hc("read", address, "--lines", "0").splitlines()[-8:]):
        if "to manage" in line:
            parts = [p.strip() for p in line.split("·")]
            return [p for p in parts if re.fullmatch(r"\d+ [a-z][a-z ]*", p)]
    return []


def scan(path, state, since):
    """Fold new transcript lines into state: live schedules, the pending wake-up, PR subscriptions."""
    crons, prs = state["crons"], state["prs"]
    with open(path, "rb") as f:
        f.seek(state["offset"])
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # a line still being written: next time
            state["offset"] += len(raw)
            line = raw.decode("utf-8", "replace")
            if '"type":"api-request' in line[:80] or not any(m in line for m in MARKS):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            at = ts(d.get("timestamp"))
            if at and at < since:
                continue  # before this process started: its timers died with the old process
            res = d.get("toolUseResult")
            if isinstance(res, dict) and "humanSchedule" in res and "id" in res:
                crons[res["id"]] = {"cron": res["humanSchedule"], "recurring": bool(res.get("recurring")),
                                    "created": at, "next": (res.get("nextFireAt") or 0) / 1000}
            if d.get("subtype") == "scheduled_task_fire" and not crons.get(d.get("taskId"), {}).get("recurring", True):
                crons.pop(d.get("taskId"), None)  # a one-shot task deletes itself when it fires
            att = d.get("attachment") or {}
            if att.get("type") == "session_cron_carry":  # Claude's own list of what is still scheduled
                live = {t["id"]: t for t in att.get("tasks") or [] if "id" in t}
                for k in [k for k in crons if k not in live]:
                    del crons[k]
                for k, t in live.items():
                    crons.setdefault(k, {"cron": t.get("cron", "?"), "recurring": t.get("recurring", True),
                                         "created": at, "next": 0})
            content = (d.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input") or {}
                if name == "CronDelete":
                    crons.pop(inp.get("id"), None)
                elif name == "ScheduleWakeup":
                    delay = inp.get("delaySeconds")
                    state["wake"] = None if inp.get("stop") or delay is None else at + min(3600, max(60, delay))
                elif name.endswith("unsubscribe_pr_activity") or name == "UnsubscribePR":
                    key = json.dumps(inp, sort_keys=True)
                    if key in prs:
                        prs.remove(key)
                elif name.endswith("subscribe_pr_activity") or name == "SubscribePR":
                    key = json.dumps(inp, sort_keys=True)
                    if key not in prs:
                        prs.append(key)


def timers(session, config_dir, cache, now):
    found = glob.glob(f"{config_dir}/projects/*/{session['sessionId']}.jsonl")
    if not found:
        return []
    path = found[0]
    state = cache.get(path)
    if not state or state.get("pid") != session["pid"] or state["offset"] > os.path.getsize(path):
        state = {"pid": session["pid"], "offset": 0, "crons": {}, "wake": None, "prs": []}
    scan(path, state, session.get("startedAt", 0) / 1000)
    cache[path] = state
    why = []
    for c in state["crons"].values():
        if c["recurring"] and now - c["created"] < CRON_EXPIRY_DAYS * 86400:
            why.append(f"schedule {c['cron']}")
        elif not c["recurring"] and c["next"] > now - 600:
            why.append("one-off " + time.strftime("%H:%M", time.localtime(c["next"])))
    if state["wake"] and state["wake"] > now - 60:
        why.append("wakes " + time.strftime("%H:%M", time.localtime(state["wake"])))
    if state["prs"]:
        why.append(f"{len(state['prs'])} PR watch" + ("es" if len(state["prs"]) > 1 else ""))
    return why


def inspect(term):
    """A terminal's Claude pid and footer counts."""
    try:
        pid = json.loads(hc("get", term["address"], "--json"))["foreground"]["pid"]
    except (ValueError, KeyError, TypeError):
        pid = None
    return term["address"], pid, footer(term["address"])


def remote(machine):
    """Another machine's answer: this same script, run there with --local, keys prefixed with its name."""
    try:
        r = subprocess.run(["hc", "exec", "-i", "--on", machine, "python3", "-", "--local"],
                           input=open(__file__).read(), capture_output=True, text=True, timeout=30)
        return {f"{machine}/{a}": w for a, w in json.loads(r.stdout).items()}
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return {}  # unreachable, no python3 there, or an older copy: its agents stay as Herdr shows them


def machines():
    try:
        return [m["name"] for m in json.loads(hc("machine", "ls", "--json"))["machines"] if m.get("state") == "connected"]
    except (ValueError, KeyError, TypeError):
        return []


def main():
    pool = ThreadPoolExecutor(max_workers=8)
    others = [] if "--local" in sys.argv else [pool.submit(remote, m) for m in machines()]
    try:
        terms = json.loads(hc("ls", "--json"))["terminals"]
    except (ValueError, KeyError):
        sys.exit("holding: hc ls failed")
    # Only where Herdr shows nothing going on; "/" in an address means another machine, which runs its own copy.
    terms = [t for t in terms if t.get("agent") == "claude-code" and t.get("status") in ("done", "idle")
             and "/" not in t["address"]]
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    known, now, out = sessions(), time.time(), {}
    seen = list(pool.map(inspect, terms))
    for address, pid, counts in seen:
        why = list(counts)
        if pid in known:
            session, config_dir = known[pid]
            status, age = session.get("status"), now - session.get("statusUpdatedAt", 0) / 1000
            if not why and status == "shell":
                why.append("shell or monitor")
            # "busy" with the turn over means delegated work; the age skips a turn that is just starting.
            if status == "busy" and age > 5 and not any("agent" in w or "workflow" in w for w in why):
                why.append("background agents")
            try:
                why += timers(session, config_dir, cache, now)
            except OSError:
                pass
        if why:
            out[address] = ", ".join(why)
    live = {glob.escape(p) for p in cache if os.path.exists(p)}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({p: s for p, s in cache.items() if p in live}, f)
    os.replace(tmp, CACHE)
    for f in others:
        out.update(f.result())
    print(json.dumps(out))


if __name__ == "__main__":
    main()
