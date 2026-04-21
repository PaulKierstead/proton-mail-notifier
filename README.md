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

## Config files

The daemon reads two YAML files:

- **`config.yaml`** — connectivity + storage: IMAP, Ollama, Pushover defaults,
  state-db path, log level. See `config.example.yaml`.
- **`rules.yaml`** — pattern DSL: the `patterns:` list. See `rules.example.yaml`.

Splitting them keeps two things apart that change at different rates: you rarely
touch the IMAP host but you might iterate on patterns weekly. In Kubernetes
they naturally become two ConfigMap keys — edit rules without reissuing the
connectivity config.

Back-compat: you can still inline `patterns:` inside `config.yaml` and skip the
separate rules file.

Flag / env mapping:

| Flag                 | Env var                   | Default                                         |
| -------------------- | ------------------------- | ----------------------------------------------- |
| `--config PATH`      | `PROTON_WATCHER_CONFIG`   | `~/.config/proton-watcher/config.yaml`          |
| `--rules PATH`       | `PROTON_WATCHER_RULES`    | unset (patterns read from main config)          |
| *(state db path)*    | `STATE_DB`                | whatever `state_db:` says in the main config    |

Secrets are environment variables only — never read from YAML:

| Env var              | What                                            |
| -------------------- | ----------------------------------------------- |
| `BRIDGE_PASSWORD`    | Proton Mail Bridge IMAP password (not your Proton account password) |
| `PUSHOVER_USER_KEY`  | Pushover user key (30-char)                     |
| `PUSHOVER_API_TOKEN` | Pushover application/API token                  |

## Install — macOS (launchd)

```bash
# 1. Lay out files
mkdir -p ~/opt/proton-watcher ~/.config/proton-watcher ~/Library/LaunchAgents
cp watcher.py run.sh requirements.txt ~/opt/proton-watcher/
cp config.example.yaml ~/.config/proton-watcher/config.yaml
cp rules.example.yaml  ~/.config/proton-watcher/rules.yaml
cp env.example ~/.config/proton-watcher/env
cp com.pkierstead.proton-watcher.plist ~/Library/LaunchAgents/
chmod +x ~/opt/proton-watcher/run.sh
chmod 600 ~/.config/proton-watcher/env

# 2. Create venv + deps
cd ~/opt/proton-watcher
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Fill in secrets + config
vi ~/.config/proton-watcher/env          # BRIDGE_PASSWORD, PUSHOVER_*
vi ~/.config/proton-watcher/config.yaml  # username, model, IMAP host
vi ~/.config/proton-watcher/rules.yaml   # your patterns

# 4. Smoke tests (run manually first, NOT via launchd, to see output)
source ~/.config/proton-watcher/env
.venv/bin/python watcher.py \
    --config ~/.config/proton-watcher/config.yaml \
    --rules  ~/.config/proton-watcher/rules.yaml \
    --test-pushover
.venv/bin/python watcher.py \
    --config ~/.config/proton-watcher/config.yaml \
    --rules  ~/.config/proton-watcher/rules.yaml \
    --test-ollama

# 5. Load the launch agent
launchctl load -w ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist

# 6. Watch the logs
tail -f /tmp/proton-watcher.out.log /tmp/proton-watcher.err.log
```

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.pkierstead.proton-watcher.plist
```

## Install — Docker

```bash
docker build -t proton-watcher:latest .

docker run --rm \
    -v "$HOME/.config/proton-watcher/config.yaml:/config/config.yaml:ro" \
    -v "$HOME/.config/proton-watcher/rules.yaml:/config/rules.yaml:ro" \
    -v proton-watcher-state:/data \
    -e BRIDGE_PASSWORD \
    -e PUSHOVER_USER_KEY \
    -e PUSHOVER_API_TOKEN \
    proton-watcher:latest
```

The image expects:

- `/config/config.yaml` and `/config/rules.yaml` — mount as read-only.
- `/data/` — writable volume for the SQLite state. `STATE_DB=/data/state.sqlite3`
  is baked in, so whatever `state_db:` says in the YAML is ignored.
- Secrets as env vars (`-e` or `--env-file`).

Inside the container `imap.host` in config.yaml must resolve. On Docker Desktop
use `host.docker.internal`; on Linux either `--network host` or a routable
address for where Proton Mail Bridge actually runs.

## Install — Kubernetes

Single-replica Deployment with a PVC for state. Because SeenStore is a SQLite
file, do not scale beyond 1 — use `strategy: Recreate` and
`accessModes: [ReadWriteOnce]` on the PVC.

Proton Mail Bridge itself is not containerized here; it needs to run somewhere
reachable by the Pod (a sidecar image you build yourself, a host elsewhere on
the network, etc.). Point `imap.host` at whatever Service / IP exposes it.

### 1. Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: proton-watcher-secrets
  namespace: proton-watcher
type: Opaque
stringData:
  BRIDGE_PASSWORD:    "paste-bridge-generated-password"
  PUSHOVER_USER_KEY:  "your-30-char-user-key"
  PUSHOVER_API_TOKEN: "your-application-api-token"
```

Or, better, use an external secrets operator / sealed secrets — the three env
names above are all the app needs.

### 2. ConfigMap (split: connectivity + rules)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: proton-watcher-config
  namespace: proton-watcher
data:
  config.yaml: |
    imap:
      host: proton-bridge.proton-watcher.svc.cluster.local
      port: 1143
      starttls: true
      username: paul@example.com
      mailboxes: [INBOX]
      idle_refresh_seconds: 540
      ssl_verify: false
    ollama:
      base_url: http://ollama.ollama.svc.cluster.local:11434
      model: gpt-oss:20b
      timeout_seconds: 45
      body_char_limit: 4000
    pushover:
      device: null
      emergency_retry: 60
      emergency_expire: 1800
      min_urgency: 7
      sound: persistent
    # state_db is overridden by STATE_DB=/data/state.sqlite3 in the image
    state_db: /data/state.sqlite3
    log_level: INFO

  rules.yaml: |
    patterns:
      - name: boss_urgent
        description: >
          Direct email from my manager or a VP asking me to do something
          today, or asking a question they need answered today. Not urgent
          if it's an FYI, CC'd thread, or newsletter.
      - name: production_incident
        description: >
          Alerts from PagerDuty, Datadog, Sentry, AWS, GCP, or internal
          monitoring indicating a production outage or SEV1/SEV2.
      # ...add your own
```

### 3. PersistentVolumeClaim (state DB)

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: proton-watcher-state
  namespace: proton-watcher
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 256Mi
  # storageClassName: standard  # uncomment and set as appropriate
```

256Mi is generous — the store is a handful of integers per message.

### 4. Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: proton-watcher
  namespace: proton-watcher
spec:
  replicas: 1
  strategy:
    type: Recreate           # SQLite single-writer; no rolling updates
  selector:
    matchLabels: {app: proton-watcher}
  template:
    metadata:
      labels: {app: proton-watcher}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000          # so the PVC mount is writable by uid 1000
      containers:
        - name: watcher
          image: ghcr.io/YOUR-ORG/proton-watcher:latest   # or your registry
          imagePullPolicy: IfNotPresent
          envFrom:
            - secretRef:
                name: proton-watcher-secrets
          # PROTON_WATCHER_CONFIG, PROTON_WATCHER_RULES, STATE_DB are set
          # in the image; no need to repeat them here.
          volumeMounts:
            - name: config
              mountPath: /config
              readOnly: true
            - name: state
              mountPath: /data
          resources:
            requests: {cpu: 20m,  memory: 64Mi}
            limits:   {cpu: 500m, memory: 256Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
      volumes:
        - name: config
          configMap:
            name: proton-watcher-config
        - name: state
          persistentVolumeClaim:
            claimName: proton-watcher-state
```

Apply:

```bash
kubectl create namespace proton-watcher
kubectl apply -f secret.yaml -f configmap.yaml -f pvc.yaml -f deployment.yaml
kubectl -n proton-watcher logs -f deploy/proton-watcher
```

Smoke tests once it's running:

```bash
kubectl -n proton-watcher exec deploy/proton-watcher -- \
    python /app/watcher.py --test-pushover
kubectl -n proton-watcher exec deploy/proton-watcher -- \
    python /app/watcher.py --test-ollama
```

## Getting the bridge password

Bridge generates its own random IMAP password per account, separate from your
Proton account password. That string is what goes into `BRIDGE_PASSWORD`.

**GUI mode** (macOS / Windows / Linux desktop):

Proton Mail Bridge → pick your account → **Mailbox Details** → copy the IMAP
password shown there.

**CLI / headless mode** (common for Linux servers, Docker, Kubernetes):

Launch Bridge with the `-c` flag to drop into its interactive shell:

```bash
protonmail-bridge -c
```

Inside the shell the flow is roughly: `login` (enter your Proton address,
password, and 2FA if enabled) → `list` (confirm the account is there) →
`info` (prints IMAP/SMTP host, port, username, and the generated password).
Command names can shift between Bridge versions — run `help` to see the
current list. See the [official Bridge CLI guide](https://proton.me/support/bridge-cli-guide)
and [ndom91's headless-mode walkthrough](https://ndo.dev/posts/headless_protonbridge)
for a worked example.

For container deployments the usual pattern is to run Bridge once interactively
(on a persistent volume for its keychain/state), grab the password from
`info`, stash it in your Kubernetes Secret / Docker `--env-file`, then run
Bridge non-interactively for subsequent starts on the same volume. Community
images like [shenxn/protonmail-bridge](https://github.com/shenxn/protonmail-bridge-docker)
and [VideoCurio/ProtonMailBridgeDocker](https://github.com/VideoCurio/ProtonMailBridgeDocker)
wrap this workflow if you don't want to build your own.

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

