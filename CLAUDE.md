# proton-watcher — agent kickstart

This file orients Claude Code when working in this repo. Keep it short. Update
it (and prune it) as the project evolves. `README.md` is for humans; this file
is for agents.

## What this is

A local macOS daemon that:

1. Holds an IMAP IDLE connection to Proton Mail Bridge on `127.0.0.1:1143`
   (STARTTLS).
2. Sends each new message to a local Ollama model via `/api/chat` with
   `format="json"` and a strict JSON schema prompt.
3. On match, fires a Pushover Emergency-priority page (`priority=2`), which
   keeps re-alerting until acknowledged.

Nothing leaves the machine except the outbound Pushover HTTPS call.

## File map

- `watcher.py` — single-file daemon. Config dataclasses, `SeenStore`
  (SQLite), `classify()` (Ollama), `page_pushover()`, `parse_rfc822()`, and
  `MailboxWorker` (one thread per mailbox, IDLE loop).
- `config.example.yaml` — canonical config shape; real config lives at
  `~/.config/proton-watcher/config.yaml`.
- `env.example` — shape of `~/.config/proton-watcher/env`. Never commit real
  secrets.
- `run.sh` — launchd-invoked wrapper; sources env and activates the venv.
- `com.pkierstead.proton-watcher.plist` — launchd agent definition.
- `requirements.txt` — `imapclient`, `requests`, `PyYAML`. Keep this short;
  resist adding deps.
- `README.md` — human install/run guide.

## Invariants — do not break

- **Stdlib + three deps only.** If you're reaching for a fourth dep, stop and
  justify it in the PR description first.
- **`select_folder(readonly=True)`.** The daemon never mutates the mailbox.
- **UID tracking keyed by `(mailbox, uidvalidity, uid)`.** If `UIDVALIDITY`
  changes, everything re-baselines — that is intentional.
- **Baseline only on first-ever sight of a `(mailbox, uidvalidity)` pair.**
  On reconnects, mail that arrived during the outage must still be
  classified. See `MailboxWorker.run` and `SeenStore.has_any`.
- **Pushover Emergency parameters.** `retry >= 30` and `expire <= 10800` are
  API hard limits; validate if you surface them in config.
- **Secrets via env only.** `BRIDGE_PASSWORD`, `PUSHOVER_USER_KEY`,
  `PUSHOVER_API_TOKEN`. Never read them from YAML.

## Style

- Python 3.11+. Type hints everywhere, including `from __future__ import
  annotations` already in place.
- Dataclasses for config. No pydantic unless we get a real schema-validation
  need.
- Structured logging via the stdlib `logging` module, named `proton-watcher`.
  Log the verdict for every classified message at `INFO`.
- Broad `except Exception` is fine in the outer worker loop (it logs and
  restarts); elsewhere catch specifically.
- Docstrings on public functions and on any non-obvious block; keep comments
  focused on *why*, not *what*.

## Running locally

```bash
# one-time
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# smoke tests (require env vars set and Bridge/Ollama running)
source ~/.config/proton-watcher/env
.venv/bin/python watcher.py --config ~/.config/proton-watcher/config.yaml --test-pushover
.venv/bin/python watcher.py --config ~/.config/proton-watcher/config.yaml --test-ollama

# full run in foreground
.venv/bin/python watcher.py --config ~/.config/proton-watcher/config.yaml

# launchd
launchctl load -w   ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist
launchctl unload    ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist
tail -f /tmp/proton-watcher.out.log /tmp/proton-watcher.err.log
```

## Verification before claiming a change works

- `python -m py_compile watcher.py`
- `python -c "import yaml; yaml.safe_load(open('config.example.yaml'))"`
- `bash -n run.sh`
- `python -c "import xml.etree.ElementTree as ET; ET.parse('com.pkierstead.proton-watcher.plist')"`
- If touching the classifier or prompt: run `--test-ollama` and eyeball the JSON.
- If touching Pushover: run `--test-pushover` (will actually wake the phone).

## Known gotchas

- Proton Mail Bridge's IMAP password is not the Proton account password —
  it's a bridge-generated app password shown in the Bridge UI.
- Bridge occasionally drops IDLE on sleep/wake. The worker handles this with
  exponential backoff (capped at 60s); don't add a shorter ceiling or you'll
  get into a hot-reconnect loop during long outages.
- `imapclient.search(["UNSEEN"])` returns `list[int]` of UIDs when
  `use_uid=True` is set on the client — it IS set. Don't confuse UIDs with
  sequence numbers.
- Ollama returns the content string inside `data["message"]["content"]`. When
  `format="json"` is set the content is valid JSON *most* of the time; the
  code defensively handles the rare parse failure and logs the raw string.
- `ProcessType = Interactive` in the plist matters on laptops — without it,
  macOS can throttle the process when on battery.

## Good next changes (ordered by leverage)

1. **Eval harness** (`tools/eval.py`): load a CSV of labeled samples from
   `fixtures/`, run `classify()` over them, report precision/recall per
   pattern. Makes prompt/model changes measurable.
2. **Model swap abstraction**: thin protocol so the classifier can target
   Ollama, an OpenAI-compatible endpoint, or a local llama.cpp server
   interchangeably. Useful for comparisons during eval.
3. **Historical-example retrieval**: embed classified messages, store in
   SQLite-VSS, pass top-k similar past verdicts as few-shot context.
4. **Tool-use verdicts**: replace the pure-JSON verdict with Ollama function
   calls (`page(urgency)`, `snooze(minutes)`, `ignore()`) and let the model
   pick the action.

Do (1) before (3) or (4). Without an eval harness, "improving the classifier"
is vibes.

## Out of scope (don't add without asking)

- Outbound mail sending, mailbox mutation (marking read, moving, deleting).
- A web UI. Logs + Pushover are the whole UI.
- Cloud-hosted anything. This project's whole point is local-only.
