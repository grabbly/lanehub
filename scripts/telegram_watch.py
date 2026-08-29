#!/usr/bin/env python3
"""Thin, stateless watcher: resume a Claude Code session when the bot is
@-mentioned in the team chat.

Division of labour:
  * LaneHub detects `@<bot_username>` server-side and keeps, per lane, both the
    wake cursor and the current Claude session id. It is the source of truth.
  * This watcher holds NO state. Per lane it just:
      1. GET  {base}/wake        -> {wake, wakeId, text, from, sessionId}
      2. claude --resume <id> -p "<text>"   (empty id => fresh session)
      3. read the new session id from claude's JSON output
      4. POST {base}/wake/ack    {wakeId, sessionId}   (reports id, consumes wake)
    The resumed session replies through its own lane's /send, exactly as its
    CLAUDE.md agent-prompt already instructs — the watcher never touches Telegram.

Because all state lives in LaneHub, the watcher is disposable: restart it, move
it, or run one process for many lanes. Sessions are stored per project directory
on the machine that runs `claude`, so one watcher covers every lane whose
project_dir is on THIS machine; lanes on other hosts get their own watcher.

Zero third-party deps (stdlib only).

Two ways to configure it:

A) One project, no file — just env vars (the easy teammate path):
     export LANEHUB_BASE=https://hub.example.com/backend
     export LANEHUB_KEY=...            # never commit
     export CLAUDE_PROJECT_DIR=/path/to/project
     python3 telegram_watch.py
   (optional: POLL_INTERVAL, CLAUDE_BIN, LANEHUB_SESSION_TOKEN_CAP)

B) Several projects on one machine — a JSON config, path via --config or
   WATCH_CONFIG (default: watch.config.json):

    {
      "poll_interval": 5,
      "claude_bin": "claude",
      "lanes": [
        {
          "base": "https://hub.example.com/backend",
          "key_env": "LANEHUB_KEY_BACKEND",   // read key from this env var (preferred)
          "project_dir": "/srv/projects/backend",
          "extra_args": ["--permission-mode", "acceptEdits"]   // optional
        },
        {
          "base": "https://hub.example.com/frontend",
          "key": "inline-key-if-you-must",     // or inline (don't commit real keys)
          "project_dir": "/srv/projects/frontend"
        }
      ]
    }

Keep real API keys OUT of the committed file — prefer "key_env".
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("lanehub.watch")

# stderr fragments that mean "that session id no longer exists" -> recover by
# starting a fresh session instead of failing on every future mention.
_STALE_SESSION_HINTS = ("no conversation", "not found", "no session", "invalid session")
# Give up (and skip) a single wake after this many consecutive failures so one
# poison message can't wedge a whole lane forever.
_MAX_WAKE_FAILS = 3

# Keep a long-lived lane from filling the model's context window: once a lane's
# resumed transcript grows past this many *estimated* tokens, start a FRESH
# session instead of resuming (the bot re-reads chat context via /feed anyway).
# Estimate is on-disk .jsonl bytes / 4 — a slight over-count (JSON overhead),
# so it trips a bit early, which is the safe direction. 0 disables the cap.
# A 1M-context model stays under ~50% at the 450000 default (override via env).
SESSION_TOKEN_CAP = int(os.environ.get("LANEHUB_SESSION_TOKEN_CAP", "450000"))
_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def session_token_estimate(project_dir: str, session_id: str) -> int:
    """Rough token size of a resumed session from its transcript on disk. Claude
    Code stores it at ~/.claude/projects/<abs-path, '/'->'-'>/<id>.jsonl.
    Returns 0 when the file can't be found — never block a wake on a bad guess."""
    if not session_id:
        return 0
    enc = os.path.abspath(project_dir).replace(os.sep, "-")
    try:
        return os.path.getsize(os.path.join(_PROJECTS_DIR, enc, f"{session_id}.jsonl")) // 4
    except OSError:
        return 0


class Lane:
    def __init__(self, raw: dict) -> None:
        self.base = str(raw["base"]).rstrip("/")
        key = raw.get("key")
        if not key and raw.get("key_env"):
            key = os.environ.get(raw["key_env"])
        if not key:
            sys.exit(f"lane {self.base}: no API key (set 'key' or a valid 'key_env')")
        self.key = key
        self.project_dir = raw.get("project_dir") or os.getcwd()
        self.claude_bin = raw.get("claude_bin")  # falls back to global
        self.extra_args = list(raw.get("extra_args") or [])
        # ephemeral, watcher-local retry bookkeeping (NOT session state)
        self._last_fail_wake: int | None = None
        self._fail_count = 0

    @property
    def name(self) -> str:
        return self.base.rsplit("/", 1)[-1] or self.base


class Config:
    def __init__(self, path: str) -> None:
        raw = self._load(path)
        self.poll_interval = float(raw.get("poll_interval", 5))
        self.claude_bin = raw.get("claude_bin", "claude")
        lanes = raw.get("lanes") or []
        if not lanes:
            sys.exit("config has no lanes")
        self.lanes = [Lane(l) for l in lanes]

    def _load(self, path: str) -> dict:
        """Config file if present; otherwise a single lane built from env vars
        (the easy one-project path — no JSON to edit)."""
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            base = os.environ.get("LANEHUB_BASE")
            if not base:
                sys.exit(
                    f"no config at {path} and LANEHUB_BASE is unset. Either set "
                    "LANEHUB_BASE/LANEHUB_KEY/CLAUDE_PROJECT_DIR for a single lane, "
                    "or create a config file (see the header of this file)."
                )
            return {
                "poll_interval": os.environ.get("POLL_INTERVAL", 5),
                "claude_bin": os.environ.get("CLAUDE_BIN", "claude"),
                "lanes": [{
                    "base": base,
                    "key": os.environ.get("LANEHUB_KEY"),
                    "project_dir": os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
                }],
            }
        except json.JSONDecodeError as exc:
            sys.exit(f"config is not valid JSON: {exc}")


def http_json(url: str, key: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Bridge-Token": key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hub_log(lane: "Lane", level: str, message: str) -> None:
    """Best-effort: mirror a watcher event to the hub (POST /{lane}/log) so it
    shows in the web UI. Never let logging break the watch loop."""
    try:
        http_json(f"{lane.base}/log", lane.key, method="POST",
                  body={"level": level, "message": message})
    except Exception:
        pass


def _run_claude(cfg: Config, lane: Lane, session_id: str, prompt: str) -> tuple[str | None, str]:
    """One claude invocation. Returns (new_session_id | None, stderr)."""
    claude_bin = lane.claude_bin or cfg.claude_bin
    cmd = [claude_bin, "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += lane.extra_args
    try:
        proc = subprocess.run(
            cmd, cwd=lane.project_dir, capture_output=True, text=True, timeout=1800
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        LOG.error("[%s] claude invocation failed: %s", lane.name, exc)
        return None, str(exc)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        LOG.error("[%s] claude exited %s: %s", lane.name, proc.returncode, stderr[:500])
        hub_log(lane, "error", f"claude exited {proc.returncode}: {stderr[:300] or '(no stderr)'}")
        return None, stderr
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        LOG.warning("[%s] could not parse claude json output; keeping session id", lane.name)
        return session_id, ""
    # Surface which model actually answered (the one that did the most work this
    # turn) — a resumed session keeps its ORIGINAL model unless --model overrides.
    mu = result.get("modelUsage") or {}
    if mu:
        primary = max(mu, key=lambda k: (mu[k].get("input_tokens", 0) + mu[k].get("output_tokens", 0)) if isinstance(mu[k], dict) else 0)
        u = result.get("usage") or {}
        ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        out = u.get("output_tokens", 0)
        dur_s = (result.get("duration_ms") or 0) / 1000.0
        cost = result.get("total_cost_usd") or 0
        # ctx / 10000 == ctx as a percentage of a 1M-token window
        summary = (f"replied via {primary} · {dur_s:.1f}s · ctx {ctx // 1000}k "
                   f"({ctx / 10000:.1f}% of 1M) · out {out} tok · ${cost:.3f}")
        LOG.info("[%s] %s", lane.name, summary)
        hub_log(lane, "info", summary)
    return result.get("session_id") or session_id, ""


def resume_session(cfg: Config, lane: Lane, session_id: str, prompt: str) -> str | None:
    """Resume `session_id` (or start fresh when empty), self-healing a stale id.
    Returns the session id to report back, or None if even a fresh run failed."""
    LOG.info("[%s] resuming session %s", lane.name, session_id or "(new)")
    new_id, stderr = _run_claude(cfg, lane, session_id, prompt)
    if new_id is None and session_id and any(h in stderr.lower() for h in _STALE_SESSION_HINTS):
        LOG.warning("[%s] session %s looks gone; starting a fresh one", lane.name, session_id)
        new_id, _ = _run_claude(cfg, lane, "", prompt)
    return new_id


def build_prompt(sender: str, text: str) -> str:
    return (
        f"You were @-mentioned in the team Telegram chat by {sender}:\n\n"
        f"{text}\n\n"
        "Read the chat via your LaneHub /feed for full context and reply through "
        "your lane's /send. Keep it short."
    )


def handle_lane(cfg: Config, lane: Lane, dry_run: bool = False) -> None:
    """Process at most one pending wake for a lane. Errors are contained here so
    one lane never takes down the others."""
    try:
        wake = http_json(f"{lane.base}/wake", lane.key)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOG.warning("[%s] wake poll failed: %s", lane.name, exc)
        return
    if not wake.get("wake"):
        return

    wake_id = wake["wakeId"]
    sender = wake.get("from") or "someone"
    text = wake.get("text") or ""
    LOG.info("[%s] mention from %s: %s", lane.name, sender, text[:120])

    if dry_run:
        LOG.info("[%s] DRY-RUN: would resume session %s and NOT ack (nothing consumed)",
                 lane.name, wake.get("sessionId") or "(new)")
        return

    hub_log(lane, "info", f"@mention from {sender}: {text[:200]}")
    resume_id = wake.get("sessionId") or ""
    if resume_id and SESSION_TOKEN_CAP:
        est = session_token_estimate(lane.project_dir, resume_id)
        if est >= SESSION_TOKEN_CAP:
            LOG.info("[%s] session %s ~%dk tok >= cap %dk — starting FRESH (context reset)",
                     lane.name, resume_id, est // 1000, SESSION_TOKEN_CAP // 1000)
            hub_log(lane, "info", f"context reset — session ~{est // 1000}k tok >= {SESSION_TOKEN_CAP // 1000}k cap; fresh session")
            resume_id = ""
    new_id = resume_session(cfg, lane, resume_id, build_prompt(sender, text))

    if new_id is None:
        # Retry a few times, then skip so a poison message can't wedge the lane.
        if lane._last_fail_wake == wake_id:
            lane._fail_count += 1
        else:
            lane._last_fail_wake, lane._fail_count = wake_id, 1
        if lane._fail_count < _MAX_WAKE_FAILS:
            LOG.warning("[%s] wake %s failed (%d/%d); will retry",
                        lane.name, wake_id, lane._fail_count, _MAX_WAKE_FAILS)
            return
        LOG.error("[%s] wake %s failed %d times; skipping it", lane.name, wake_id, _MAX_WAKE_FAILS)

    lane._last_fail_wake, lane._fail_count = None, 0
    ack = {"wakeId": wake_id}
    if new_id:
        ack["sessionId"] = new_id
    try:
        http_json(f"{lane.base}/wake/ack", lane.key, method="POST", body=ack)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOG.warning("[%s] ack failed: %s (wake will re-fire)", lane.name, exc)


def print_status(cfg: Config) -> None:
    """Read each lane's wake state from LaneHub (GET /info) and print it — a
    server-side view of what the watcher works against, from any machine."""
    for lane in cfg.lanes:
        try:
            info = http_json(f"{lane.base}/info", lane.key)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"{lane.name:<16} ERROR: {exc}")
            continue
        w = info.get("wake") or {}
        pending = w.get("pendingMention")
        pending_s = f"yes (wake {pending['wakeId']} from {pending['from']})" if pending else "no"
        print(f"{lane.name:<16} bot=@{info.get('botUsername')} "
              f"armed={w.get('armed')} cursor={w.get('cursor')} "
              f"session={w.get('claudeSessionId') or '(none)'} pending={pending_s}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="LaneHub @mention -> Claude Code session watcher")
    parser.add_argument(
        "--config",
        default=os.environ.get("WATCH_CONFIG", "watch.config.json"),
        help="path to the JSON config (default: watch.config.json or $WATCH_CONFIG)",
    )
    parser.add_argument("--status", action="store_true",
                        help="print each lane's wake state (from LaneHub) and exit")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll pass over all lanes and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would happen but never run claude or ack anything")
    args = parser.parse_args()
    cfg = Config(args.config)

    if args.status:
        print_status(cfg)
        return

    mode = "dry-run" if args.dry_run else ("once" if args.once else "watching")
    LOG.info(
        "%s %d lane(s) every %.1fs: %s",
        mode, len(cfg.lanes), cfg.poll_interval, ", ".join(l.name for l in cfg.lanes),
    )
    while True:
        for lane in cfg.lanes:
            handle_lane(cfg, lane, dry_run=args.dry_run)
        if args.once:
            return
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
