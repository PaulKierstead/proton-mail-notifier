#!/usr/bin/env python3
"""
proton-watcher: IMAP IDLE daemon that classifies inbound mail via a local
Ollama model and pages the user on matches using Pushover Emergency priority.

Pipeline per message:
    IMAP IDLE -> fetch -> parse -> Ollama classifier (JSON-mode) -> Pushover

Design notes
------------
* IDLE is per-mailbox. We spawn one worker thread per configured mailbox.
* imapclient's idle_check() blocks for up to `idle_timeout` seconds; we
  refresh the IDLE every ~9 minutes (RFC recommends < 29 min) and reconnect
  cleanly on any socket exception.
* The Ollama call uses /api/chat with format="json" so we can rely on strict
  JSON parsing without post-hoc regex scraping.
* A small SQLite file remembers UIDs we've already processed so a daemon
  restart doesn't re-page you on the same mail.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import json
import logging
import os
import queue
import signal
import sqlite3
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
import yaml
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

LOG = logging.getLogger("proton-watcher")

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


@dataclass
class ImapCfg:
    host: str
    port: int
    starttls: bool
    username: str
    password: str  # resolved from env at load time
    mailboxes: list[str]
    idle_refresh_seconds: int = 540  # 9 min; < 29 min per RFC 2177
    ssl_verify: bool = False  # Proton Mail Bridge uses a self-signed cert


@dataclass
class OllamaCfg:
    base_url: str
    model: str
    timeout_seconds: int
    body_char_limit: int = 4000


@dataclass
class PushoverCfg:
    user_key: str
    api_token: str
    device: str | None
    emergency_retry: int
    emergency_expire: int
    min_urgency: int
    sound: str


@dataclass
class Pattern:
    name: str
    description: str
    min_urgency: int | None = None  # overrides pushover.min_urgency when set


@dataclass
class Config:
    imap: ImapCfg
    ollama: OllamaCfg
    pushover: PushoverCfg
    patterns: list[Pattern]
    state_db: Path
    log_level: str

    @staticmethod
    def _env(name: str, required: bool = True, default: str | None = None) -> str | None:
        val = os.environ.get(name, default)
        if required and not val:
            raise SystemExit(f"Missing required environment variable: {name}")
        return val

    @classmethod
    def load(cls, path: Path, rules_path: Path | None = None) -> "Config":
        """Load config from `path`. If `rules_path` is given, patterns come from
        that file instead of the `patterns:` block in the main config. This lets
        connectivity config and the rules DSL live in separate ConfigMaps in k8s.
        """
        with path.open("r") as fh:
            raw = yaml.safe_load(fh)

        if rules_path is not None:
            with rules_path.open("r") as fh:
                rules_raw = yaml.safe_load(fh) or {}
            patterns_raw = rules_raw.get("patterns")
            if not patterns_raw:
                raise SystemExit(f"No 'patterns:' found in rules file: {rules_path}")
        else:
            patterns_raw = raw.get("patterns")
            if not patterns_raw:
                raise SystemExit(
                    "No patterns configured. Add a 'patterns:' block to the config "
                    "or pass --rules pointing at a separate rules file."
                )

        # STATE_DB env var overrides the config value — handy for containers
        # where the path is dictated by the PVC mount, not the baked config.
        state_db_raw = os.environ.get("STATE_DB") or raw.get(
            "state_db", "~/.local/state/proton-watcher/state.sqlite3"
        )

        imap = raw["imap"]
        ollama = raw["ollama"]
        push = raw["pushover"]

        return cls(
            imap=ImapCfg(
                host=imap.get("host", "127.0.0.1"),
                port=int(imap.get("port", 1143)),
                starttls=bool(imap.get("starttls", True)),
                username=imap["username"],
                password=cls._env("BRIDGE_PASSWORD"),
                mailboxes=imap.get("mailboxes", ["INBOX"]),
                idle_refresh_seconds=int(imap.get("idle_refresh_seconds", 540)),
                ssl_verify=bool(imap.get("ssl_verify", False)),
            ),
            ollama=OllamaCfg(
                base_url=ollama.get("base_url", "http://127.0.0.1:11434").rstrip("/"),
                model=ollama.get("model", "llama3.1:8b"),
                timeout_seconds=int(ollama.get("timeout_seconds", 45)),
                body_char_limit=int(ollama.get("body_char_limit", 4000)),
            ),
            pushover=PushoverCfg(
                user_key=cls._env("PUSHOVER_USER_KEY"),
                api_token=cls._env("PUSHOVER_API_TOKEN"),
                device=push.get("device") or None,
                emergency_retry=int(push.get("emergency_retry", 60)),
                emergency_expire=int(push.get("emergency_expire", 1800)),
                min_urgency=int(push.get("min_urgency", 7)),
                sound=push.get("sound", "persistent"),
            ),
            patterns=[
                Pattern(
                    name=p["name"],
                    description=p["description"],
                    min_urgency=int(p["min_urgency"]) if "min_urgency" in p else None,
                )
                for p in patterns_raw
            ],
            state_db=Path(os.path.expanduser(state_db_raw)),
            log_level=raw.get("log_level", "INFO").upper(),
        )


# ----------------------------------------------------------------------------
# State (already-seen UIDs)
# ----------------------------------------------------------------------------


class SeenStore:
    """Tiny SQLite-backed set of (mailbox, uidvalidity, uid) we've classified."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen (
                    mailbox      TEXT NOT NULL,
                    uidvalidity  INTEGER NOT NULL,
                    uid          INTEGER NOT NULL,
                    ts           INTEGER NOT NULL,
                    PRIMARY KEY (mailbox, uidvalidity, uid)
                )
                """
            )
            self._db.commit()

    def contains(self, mailbox: str, uidvalidity: int, uid: int) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM seen WHERE mailbox=? AND uidvalidity=? AND uid=? LIMIT 1",
                (mailbox, uidvalidity, uid),
            )
            return cur.fetchone() is not None

    def has_any(self, mailbox: str, uidvalidity: int) -> bool:
        """True once we've ever recorded a UID under this (mailbox, uidvalidity)."""
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM seen WHERE mailbox=? AND uidvalidity=? LIMIT 1",
                (mailbox, uidvalidity),
            )
            return cur.fetchone() is not None

    def add(self, mailbox: str, uidvalidity: int, uid: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO seen (mailbox, uidvalidity, uid, ts) VALUES (?, ?, ?, ?)",
                (mailbox, uidvalidity, uid, int(time.time())),
            )
            self._db.commit()


# ----------------------------------------------------------------------------
# Ollama classifier
# ----------------------------------------------------------------------------


CLASSIFIER_SYSTEM_PROMPT = """\
You are an email triage classifier. You receive one email and a list of
user-defined urgency patterns, and you must decide whether the email matches
any pattern. You MUST return strict JSON matching this schema and nothing else:

{
  "match": boolean,
  "pattern": string | null,     // exact pattern name (the text in [brackets] below), or null
  "urgency": integer,           // 1 (trivial) to 10 (drop everything now)
  "reason": string,             // one concise sentence explaining the verdict
  "action_hint": string         // <= 12 words: what the user likely needs to do, or "" if no match
}

Rules:
- Be conservative: only set match=true when the email clearly fits a pattern.
- Marketing, newsletters, notifications without action required, and automated
  receipts are almost never urgent. Do not page the user on these.
- Calendar updates are only urgent if they conflict imminently or are flagged
  as same-day by the sender.
- Thread replies from the user's own manager or direct asks with a deadline
  today are high urgency (8-10).
- Return urgency independent of match; a non-matching email may still be
  urgency 3-5. Only match=true triggers paging.
- Output JSON only. No prose, no markdown, no code fences.
"""


def classify(
    cfg: OllamaCfg,
    patterns: list[Pattern],
    headers: dict[str, str],
    body_text: str,
) -> dict[str, Any]:
    patterns_block = "\n".join(f"- [{p.name}] {p.description}" for p in patterns)
    body = body_text[: cfg.body_char_limit]

    user_prompt = (
        f"Patterns:\n{patterns_block}\n\n"
        f"Email headers:\n"
        f"From: {headers.get('from', '')}\n"
        f"To: {headers.get('to', '')}\n"
        f"Subject: {headers.get('subject', '')}\n"
        f"Date: {headers.get('date', '')}\n"
        f"List-Id: {headers.get('list-id', '')}\n"
        f"Auto-Submitted: {headers.get('auto-submitted', '')}\n\n"
        f"Body (truncated to {cfg.body_char_limit} chars):\n{body}\n"
    )

    payload = {
        "model": cfg.model,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 8192,
        },
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp = requests.post(
        f"{cfg.base_url}/api/chat",
        json=payload,
        timeout=cfg.timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data.get("message", {}).get("content", "").strip()

    try:
        verdict = json.loads(content)
    except json.JSONDecodeError:
        LOG.warning("Classifier returned non-JSON, raw=%r", content[:300])
        return {"match": False, "pattern": None, "urgency": 0, "reason": "parse_error", "action_hint": ""}

    # Defensive normalisation.
    verdict.setdefault("match", False)
    verdict.setdefault("pattern", None)
    try:
        verdict["urgency"] = int(verdict.get("urgency", 0))
    except (TypeError, ValueError):
        verdict["urgency"] = 0
    verdict.setdefault("reason", "")
    verdict.setdefault("action_hint", "")
    return verdict


# ----------------------------------------------------------------------------
# Pushover emergency paging
# ----------------------------------------------------------------------------


def page_pushover(
    cfg: PushoverCfg,
    title: str,
    message: str,
    url: str | None = None,
    url_title: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "token": cfg.api_token,
        "user": cfg.user_key,
        "title": title[:250],
        "message": message[:1024],
        "priority": 2,
        "retry": cfg.emergency_retry,
        "expire": cfg.emergency_expire,
        "sound": cfg.sound,
    }
    if cfg.device:
        payload["device"] = cfg.device
    if url:
        payload["url"] = url
    if url_title:
        payload["url_title"] = url_title[:100]

    resp = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=15)
    if resp.status_code != 200:
        LOG.error("Pushover failed: %s %s", resp.status_code, resp.text)
        return
    body = resp.json()
    if body.get("status") != 1:
        LOG.error("Pushover non-OK body: %s", body)
    else:
        LOG.info("Pushover accepted receipt=%s", body.get("receipt"))


# ----------------------------------------------------------------------------
# Email parsing
# ----------------------------------------------------------------------------


def parse_rfc822(raw: bytes) -> tuple[dict[str, str], str]:
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]

    headers = {k.lower(): str(v) for k, v in msg.items()}
    # Prefer text/plain, fall back to stripped text/html.
    body_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                try:
                    body_parts.append(part.get_content())
                except Exception:  # noqa: BLE001
                    pass
        if not body_parts:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        html = part.get_content()
                        body_parts.append(_strip_html(html))
                    except Exception:  # noqa: BLE001
                        pass
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:  # noqa: BLE001
            body_parts.append(raw.decode("utf-8", errors="replace"))

    return headers, "\n".join(body_parts).strip()


def _strip_html(html: str) -> str:
    # Deliberately dependency-light: a naive strip is fine for classifier input.
    import re

    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ----------------------------------------------------------------------------
# IMAP worker
# ----------------------------------------------------------------------------


class MailboxWorker(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        mailbox: str,
        seen: SeenStore,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"imap-{mailbox}", daemon=True)
        self.cfg = cfg
        self.mailbox = mailbox
        self.seen = seen
        self.stop_event = stop_event

    # -- IMAP connection lifecycle ------------------------------------------

    def _connect(self) -> IMAPClient:
        ctx = ssl.create_default_context()
        if not self.cfg.imap.ssl_verify:
            # Bridge uses a self-signed cert; disable verification for localhost.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client = IMAPClient(
            host=self.cfg.imap.host,
            port=self.cfg.imap.port,
            ssl=False,
            use_uid=True,
        )
        if self.cfg.imap.starttls:
            client.starttls(ssl_context=ctx)
        client.login(self.cfg.imap.username, self.cfg.imap.password)
        client.select_folder(self.mailbox, readonly=True)
        return client

    def _uidvalidity(self, client: IMAPClient) -> int:
        status = client.folder_status(self.mailbox, [b"UIDVALIDITY"])
        return int(status[b"UIDVALIDITY"])

    # -- core loop ----------------------------------------------------------

    def run(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                LOG.info("[%s] connecting to %s:%d", self.mailbox, self.cfg.imap.host, self.cfg.imap.port)
                with self._connect() as client:
                    uidvalidity = self._uidvalidity(client)
                    LOG.info("[%s] connected, uidvalidity=%d", self.mailbox, uidvalidity)

                    # First-ever connect to this (mailbox, uidvalidity): mark all
                    # currently-existing UIDs as "seen" without classifying, so we
                    # don't page on historical mail. On subsequent reconnects we
                    # SKIP this pass so mail that arrived during the outage is
                    # still classified on reconnect.
                    if not self.seen.has_any(self.mailbox, uidvalidity):
                        existing = client.search(["ALL"])
                        for uid in existing:
                            self.seen.add(self.mailbox, uidvalidity, int(uid))
                        LOG.info(
                            "[%s] first-time baseline: %d UIDs marked seen (uidvalidity=%d)",
                            self.mailbox, len(existing), uidvalidity,
                        )
                    else:
                        LOG.info("[%s] reconnect; skipping baseline pass", self.mailbox)

                    backoff = 2.0  # reset after successful connect
                    self._idle_loop(client, uidvalidity)
            except (IMAPClientError, OSError, ssl.SSLError) as exc:
                LOG.warning("[%s] connection lost: %s; reconnecting in %.1fs", self.mailbox, exc, backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception:  # noqa: BLE001
                LOG.exception("[%s] fatal worker error; restarting", self.mailbox)
                self.stop_event.wait(5.0)

    def _idle_loop(self, client: IMAPClient, uidvalidity: int) -> None:
        while not self.stop_event.is_set():
            client.idle()
            try:
                # Wake on server push OR every idle_refresh_seconds, whichever first.
                responses = client.idle_check(timeout=self.cfg.imap.idle_refresh_seconds)
            finally:
                client.idle_done()

            if responses:
                LOG.debug("[%s] IDLE responses: %s", self.mailbox, responses)

            # Even if the list is empty (timeout refresh), do a pass: IMAP servers
            # occasionally swallow EXISTS notifications under load.
            new_uids = self._fetch_new_uids(client, uidvalidity)
            for uid in new_uids:
                if self.stop_event.is_set():
                    return
                self._handle_uid(client, uidvalidity, uid)

    def _fetch_new_uids(self, client: IMAPClient, uidvalidity: int) -> list[int]:
        # Cheap: ask for recent UNSEEN; prune anything already in the seen store.
        uids = client.search(["UNSEEN"])
        fresh = [int(u) for u in uids if not self.seen.contains(self.mailbox, uidvalidity, int(u))]
        return fresh

    def _handle_uid(self, client: IMAPClient, uidvalidity: int, uid: int) -> None:
        try:
            resp = client.fetch([uid], ["RFC822"])
            raw = resp.get(uid, {}).get(b"RFC822")
            if not raw:
                LOG.warning("[%s] uid=%d returned no RFC822", self.mailbox, uid)
                self.seen.add(self.mailbox, uidvalidity, uid)
                return

            headers, body = parse_rfc822(raw)
            LOG.info(
                "[%s] classifying uid=%d from=%r subject=%r",
                self.mailbox,
                uid,
                headers.get("from", ""),
                headers.get("subject", ""),
            )

            verdict = classify(self.cfg.ollama, self.cfg.patterns, headers, body)
            LOG.info("[%s] verdict uid=%d: %s", self.mailbox, uid, verdict)

            raw_vp = (verdict.get("pattern") or "").strip()
            # If the model echoed the full "[Name] description" line, extract the name.
            if raw_vp.startswith("[") and "]" in raw_vp:
                raw_vp = raw_vp[1:raw_vp.index("]")]
            verdict_pattern = raw_vp.lower()
            matched_pattern = next(
                (p for p in self.cfg.patterns if p.name.strip().lower() == verdict_pattern),
                None,
            )
            threshold = (
                matched_pattern.min_urgency
                if matched_pattern is not None and matched_pattern.min_urgency is not None
                else self.cfg.pushover.min_urgency
            )
            LOG.debug(
                "[%s] uid=%d pattern_match=%s threshold=%d",
                self.mailbox, uid, matched_pattern.name if matched_pattern else None, threshold,
            )
            if verdict.get("match") and int(verdict.get("urgency", 0)) >= threshold:
                title = f"[{verdict.get('pattern') or 'urgent'}] {headers.get('subject', '(no subject)')}"[:250]
                action = verdict.get("action_hint") or ""
                msg = (
                    f"From: {headers.get('from', '')}\n"
                    f"Urgency: {verdict.get('urgency')}/10\n"
                    f"Why: {verdict.get('reason', '')}\n"
                    + (f"Action: {action}\n" if action else "")
                )
                page_pushover(self.cfg.pushover, title=title, message=msg)
        finally:
            # Always remember we looked at it, even on classifier error, so we
            # don't page repeatedly for a broken message.
            self.seen.add(self.mailbox, uidvalidity, uid)


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------


def _load_env_file() -> None:
    """Load KEY=value pairs from .env or env next to this script, if present.

    Only sets variables not already in the environment, so launchd/shell-sourced
    values always win. Handles `export KEY=value` and bare `KEY=value` lines;
    strips inline comments and optional surrounding quotes.
    """
    script_dir = Path(__file__).parent
    for name in (".env", "env"):
        candidate = script_dir / name
        if candidate.is_file():
            loaded: list[str] = []
            with candidate.open() as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    line = line.removeprefix("export").strip()
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().split("#")[0].strip()  # drop inline comments
                    if val and val[0] in ('"', "'") and val[-1] == val[0]:
                        val = val[1:-1]
                    if key and key not in os.environ:
                        os.environ[key] = val
                        loaded.append(key)
            if loaded:
                # Use print; logging isn't configured yet at this point.
                print(f"proton-watcher: loaded {len(loaded)} var(s) from {candidate}", flush=True)
            return  # stop after first file found


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s[%(threadName)s] %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="proton-watcher: urgent-mail pager via Ollama + Pushover")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.path.expanduser(
            os.environ.get("PROTON_WATCHER_CONFIG", "~/.config/proton-watcher/config.yaml")
        )),
        help="Path to main YAML config (connectivity, pushover, storage). "
             "Defaults to $PROTON_WATCHER_CONFIG or ~/.config/proton-watcher/config.yaml.",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=(
            Path(os.path.expanduser(os.environ["PROTON_WATCHER_RULES"]))
            if os.environ.get("PROTON_WATCHER_RULES")
            else None
        ),
        help="Optional separate YAML file containing just a 'patterns:' list. "
             "Defaults to $PROTON_WATCHER_RULES if set; otherwise patterns are "
             "read from the main config.",
    )
    parser.add_argument("--test-pushover", action="store_true", help="Send a test emergency page and exit")
    parser.add_argument("--test-ollama", action="store_true", help="Run classifier on a canned message and exit")
    args = parser.parse_args(argv)

    _load_env_file()
    cfg = Config.load(args.config, rules_path=args.rules)
    _setup_logging(cfg.log_level)
    LOG.info("Loaded config: mailboxes=%s model=%s patterns=%d", cfg.imap.mailboxes, cfg.ollama.model, len(cfg.patterns))

    if args.test_pushover:
        page_pushover(cfg.pushover, title="proton-watcher test", message="Emergency priority test page.")
        return 0

    if args.test_ollama:
        headers = {
            "from": "boss@example.com",
            "to": cfg.imap.username,
            "subject": "Need your sign-off before EOD today",
            "date": "Thu, 16 Apr 2026 10:15:00 -0400",
        }
        body = "Hi Paul, need your approval on the Q2 plan before 5pm today. Please reply when you get this."
        verdict = classify(cfg.ollama, cfg.patterns, headers, body)
        print(json.dumps(verdict, indent=2))
        return 0

    seen = SeenStore(cfg.state_db)
    stop = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        LOG.info("signal %d received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    workers = [MailboxWorker(cfg, mb, seen, stop) for mb in cfg.imap.mailboxes]
    for w in workers:
        w.start()

    # Main thread just supervises.
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        LOG.info("stopping workers...")
        stop.set()
        for w in workers:
            w.join(timeout=10.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
