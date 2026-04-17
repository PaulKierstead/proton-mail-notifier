# proton-mail-watcher

NOTE: The install stuff is untest. I just run python from a venv currently.

Very first version and proof of concept.

Proton mail presents some challenges when you want to process your incoming 
mail using an AI agent. You certainly can't let your Claude co-work loose on
your account, etc. Well, it would be kind of self defeating even if there
was an MCP and keys provided. I wanted a little local AI processing of mail,
in this instance i wanted more loose rules to be used to process incoming
mail and send high priority alerts. This allows it.

Uses Proton Bridge to read mail ollama for AI services (can be local or 
cloud behind it) and pushover for notifications.

Mostly vibe coded with claude and claude code, and the first cut.

My future for this is to actually pull the mail into something like kafka
where multiple processes can pull new mail for archiving, indexing and
alerting.

# Description

Local-only urgent-mail pager. Watches a Proton Mail Bridge IMAP mailbox via
IDLE, classifies each new message with a local Ollama model, and fires a
Pushover **Emergency-priority** page (retries until you ack) when a user-
defined pattern matches.

Nothing leaves your machine except the outbound Pushover HTTPS call.

## Architecture

```
Proton Mail Bridge (127.0.0.1:1143, STARTTLS)
          │ IMAP IDLE
          ▼
   watcher.py (launchd)
          │ /api/chat  format=json
          ▼
   Ollama (127.0.0.1:11434)
          │ verdict {match, pattern, urgency, reason, action_hint}
          ▼
   Pushover Emergency (priority=2)   ──► phone screams until you ack
```

One worker thread per mailbox, each holding an IDLE connection. UIDs are
remembered in a tiny SQLite store (`state.sqlite3`) so restarts don't re-page
you on historical mail; on first connect to a mailbox the watcher establishes
a baseline of existing UIDs, then only classifies genuinely new arrivals.

## Install

```bash
# 1. Lay out files
mkdir -p ~/opt/proton-watcher ~/.config/proton-watcher ~/Library/LaunchAgents
cp watcher.py run.sh requirements.txt ~/opt/proton-watcher/
cp config.example.yaml ~/.config/proton-watcher/config.yaml
cp env.example ~/.config/proton-watcher/env
cp com.pkierstead.proton-watcher.plist ~/Library/LaunchAgents/
chmod +x ~/opt/proton-watcher/run.sh
chmod 600 ~/.config/proton-watcher/env

# 2. Create venv + deps
cd ~/opt/proton-watcher
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Fill in secrets
#   ~/.config/proton-watcher/env         <- BRIDGE_PASSWORD, PUSHOVER_*
#   ~/.config/proton-watcher/config.yaml <- username, patterns, model
vi ~/.config/proton-watcher/env
vi ~/.config/proton-watcher/config.yaml

# 4. Smoke tests (run manually first, NOT via launchd, to see output)
source ~/.config/proton-watcher/env
.venv/bin/python watcher.py --config ~/.config/proton-watcher/config.yaml --test-pushover
.venv/bin/python watcher.py --config ~/.config/proton-watcher/config.yaml --test-ollama

# 5. Load the launch agent
launchctl load -w ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist

# 6. Watch the logs
tail -f /tmp/proton-watcher.out.log /tmp/proton-watcher.err.log
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist
```

## Getting the bridge password

Proton Mail Bridge → pick your account → **Mailbox Details** → copy the IMAP
password shown there. That string (not your Proton account password) goes into
`BRIDGE_PASSWORD`.

## Getting Pushover credentials

1. Install the Pushover app on your phone, pay the one-time license (~$5), sign in.
2. Your **User Key** is shown on the Pushover website after login.
3. Create an **Application/API Token**: Pushover site → "Create an Application/API Token" → name it "proton-watcher" → copy the token.
4. On iOS, grant the Pushover app the **Critical Alerts** permission if you want
   Emergency pages to bypass silent mode / DND. (iOS Settings → Notifications →
   Pushover → Critical Alerts.)

Emergency priority (`priority=2`) means Pushover re-alerts every
`emergency_retry` seconds until you tap acknowledge on any device, or until
`emergency_expire` seconds have passed. Defaults here: retry every 60s, give
up after 30 min.

## Choosing an Ollama model

The classifier task is short-context, mostly English, with strict JSON output.
You don't need anything huge:

| Model               | Size   | Notes                                             |
| ------------------- | ------ | ------------------------------------------------- |
| `llama3.1:8b`       | ~4.7GB | Default. Reliable JSON output, solid reasoning.   |
| `qwen2.5:7b`        | ~4.4GB | Often better instruction-following than llama3.1. |
| `mistral-nemo:12b`  | ~7GB   | Strong if you have the VRAM.                      |
| `phi4:14b`          | ~9GB   | Smart but slower per token.                       |
| `llama3.2:3b`       | ~2GB   | Fastest; adequate for obvious matches only.       |

:Switch with `ollama pull <model>` then update `ollama.model` in config.yaml.

If you already have Open WebUI running against Ollama, no change is needed —
this daemon talks to Ollama directly at `http://127.0.0.1:11434`.

## The pattern DSL

Each pattern in `config.yaml` has a stable `name` and a natural-language
`description`. The description is what the model actually matches against, so
write it the way you'd brief a new assistant:

- Be specific about who counts ("my manager", "anyone @mycompany.com", "family").
- Say what *does NOT* count ("not newsletters", "not automated receipts").
- Mention domain cues the model can use (sender patterns, subject markers).

The verdict includes an `urgency` score 1–10 independent of match; only
`match=true` AND `urgency >= pushover.min_urgency` triggers a page. Tune
`min_urgency` up if you get paged too often, down if the model is under-rating.

## Tuning & debugging

- **Log everything**: set `log_level: DEBUG` in config.yaml. Every verdict is
  logged, so you can see *why* a message was or wasn't paged.
- **Replay a specific UID**: pull the raw RFC822 with any IMAP client, save
  to `sample.eml`, then from a Python REPL:
  ```python
  from watcher import Config, classify, parse_rfc822
  cfg = Config.load(Path("~/.config/proton-watcher/config.yaml").expanduser())
  headers, body = parse_rfc822(open("sample.eml","rb").read())
  print(classify(cfg.ollama, cfg.patterns, headers, body))
  ```
- **Iterate the system prompt**: `CLASSIFIER_SYSTEM_PROMPT` in `watcher.py` is
  where you shape the model's judgment. Once you have 10–20 labeled samples,
  you can turn this into an eval loop — that's the obvious next step for the
  AI-skills angle.

## Known limitations

- IMAP IDLE reconnects are exponential-backoff, capped at 60s. If the Bridge
  is bounced you'll see a brief gap.
- `readonly=True` — the daemon never marks messages read or moves them. It
  doesn't touch your mailbox state at all.
- HTML-only emails are stripped with a naive regex; that's fine for
  classification input but will look ugly if you ever log the body.
- First startup classifies nothing (baseline pass). If you want to replay
  the last 24h on first run, it's a small change in `MailboxWorker.run`.

