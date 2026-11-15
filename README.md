# herdr-mod-agents-holding

The [Herdr](https://herdr.dev) desktop app shows a Claude Code agent as done or idle as soon as
its turn ends, even when it still holds a monitor, a background shell or a schedule. This mod
adds a "Waiting" section to the Agents list for those agents, with the reason on each row
("1 monitor", "schedule", "wakes 14:30").

Verified against Claude Code 2.1.323. Every source is a Claude Code internal and may change.
Agents on other machines too, if they have `python3` and `hc` (see below).

## Install

```sh
git clone https://github.com/trapped/herdr-mod-agents-holding ~/.config/herdr-app/mods/agents-holding
python3 ~/.config/herdr-app/mods/agents-holding/sync.py
herdr-app do layout.switch '{"name":"main-mods"}'
```

The folder must be called `agents-holding`.

## The rule

An agent is "waiting" only on positive evidence that it holds something, and the row says what.
Everything else stays as Herdr reports it. If a source stops working, rows fall back to idle.

## Cases and sources

| # | Case | Source | Tested |
|---|---|---|---|
| 1 | Background shell, or a monitor that runs a shell command | session file `status: shell`; the footer says which ("1 monitor", "1 shell") | yes |
| 2 | Background sub-agents, workflows, teammates, cloud agents | session file `status: busy` while Herdr shows done or idle | no, read from the code |
| 3 | A dialog or question is open | not handled: Herdr shows "Needs you" | - |
| 4 | Scheduled task, session-only | transcript: `toolUseResult` of `CronCreate`, `CronDelete`, `scheduled_task_fire`, `session_cron_carry` | yes |
| 5 | One-off wake-up | transcript: `ScheduleWakeup` (time of call + `delaySeconds`) | no |
| 6 | Scheduled task saved to disk (`durable: true`) | transcript, as case 4, when this session created it | no |
| 7 | Monitor that runs no shell (MCP, websocket), MCP task, long-running cloud agent | **gap**, see below | no |
| 8 | Pull request subscription | transcript: `SubscribePR` / `subscribe_pr_activity` minus unsubscribes | no |

- Session file: `<config dir>/sessions/<pid>.json`, for `$CLAUDE_CONFIG_DIR` and every
  `~/.claude*` that has a `sessions` folder.
- Transcript: `<config dir>/projects/*/<sessionId>.jsonl`. Only entries after the process started
  count, because session timers die with the process. Recurring schedules expire after 7 days.
- Footer: the `N monitor · N shell · ↓ to manage` line, read with `hc read <t> --lines 0`.

## Other machines

`holding.py` runs itself on every connected machine from `hc machine ls` (`hc exec --on <machine> python3 - --local`, script on stdin, nothing installed there) and merges the answers as `<machine>/<terminal>`. Each machine reads its own session files, transcripts and footers.

## Known gaps

- **Case 7 is a gap.** Claude Code leaves these task types (`monitor_mcp`, `monitor_ws`,
  `mcp_task`, long-running `remote_agent`) out of the session status on purpose, and writes
  nothing about them to the transcript. The footer count is the only place they could show, and
  that is untested because none has been seen in use. Address this as soon as Claude Code
  records them: check the session file's `status` values and `holding.py`'s footer parsing
  against a real one.
- A saved schedule (case 6) created by an earlier session and picked up by this one is not seen.
- Agents on other machines need `python3` and `hc` there, and a connected `hc machine`. A machine that can't be reached, or lacks python3, shows its agents as Herdr does.
- Not detected at all, because nothing is running: an agent waiting on a person, a turn cut short
  by an error or a limit, and unsent text in the prompt box.

## How it is built

Herdr replaces its shipped mods and its `main` layout on every start, and never updates copies.
So nothing here is a copy:

- `patch.py` builds this mod's `main.js` and `mod.json` from Herdr's shipped `agents` mod plus a
  few exact edits. If an edit no longer fits (Herdr changed that code), nothing is written.
- `sync.py` builds `layouts/main-mods.json`: Herdr's current `main` plus the edit in every mod's
  `layout.json`. Here that edit runs `patch.py` and shows this mod in place of `agents`. If
  `patch.py` fails, the layout keeps Herdr's own list.
- The mod runs `sync.py` on every start, so both follow Herdr updates.

## Files

| File | What it does |
|---|---|
| `holding.py` | prints `{"<terminal>": "<why>"}` for local Claude agents Herdr shows done or idle; keeps its place in each transcript in `~/.cache/herdr-agents-holding.json` |
| `patch.py` | generates `main.js` and `mod.json` (both git-ignored) |
| `layout.json` | the layout edit |
| `sync.py` | builds `layouts/main-mods.json` |
