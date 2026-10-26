# Using this fork on an existing Odysseus install

This fork is Danish's copy of [Odysseus](https://github.com/odysseus-dev/odysseus). The change in this branch makes scheduled routines and other background work reliable: they can run while you are chatting, they survive a restart, and an approval in a background task waits for you instead of being thrown away.

Your existing chats, tasks, and mail stay in the `data/` folder. Do not delete that folder when you update.

## Which install should you use?

If Odysseus is already running with Docker Compose, stay on Docker. Build the image from this fork and keep the same `./data` volume. That is the better fit for a single-user assistant: Chroma and SearXNG are already wired up, upgrades are one command, and you do not have to keep a Python environment in sync with the container.

A native Python install is feasible, and a hybrid is reasonable when you are editing the code and do not want to rebuild the image for every change. Do not run the Docker app and a native `uvicorn` against the same `data/app.db` at the same time. SQLite allows one writer.

## A. Docker: build this fork and keep your data

From the directory that already has your `docker-compose.yml` and `./data`:

1. Stop the app, but do not remove volumes.

   ```bash
   docker compose stop odysseus
   ```

2. Point the checkout at this fork (or copy this fork's files over the existing checkout). Leave `./data` where it is. `docker-compose.yml` already has `build: .` and mounts `./data` into the container.

3. Build the local image instead of pulling `ghcr.io/odysseus-dev/odysseus`. An empty `ODYSSEUS_IMAGE` makes Compose use the image name it builds:

   ```bash
   ODYSSEUS_IMAGE=odysseus:fork docker compose build odysseus
   ODYSSEUS_IMAGE=odysseus:fork docker compose up -d
   ```

   If you previously pinned `ODYSSEUS_IMAGE` to a `ghcr.io` tag, change it to the local tag above. Otherwise Docker keeps using the upstream image and you will not get this fork.

4. On startup the app runs its database migrations, including the new columns and the `background_jobs` table. You do not run a separate migration command. The first start after the update can take a little longer.

5. Later updates are the same build and `up -d`. Your `./data` volume, the Chroma volume, and SearXNG's volume stay in place.

`docker compose up` does not start an extra worker. The app process still fires routines, which is what you want for one machine. The optional worker is only for splitting that loop out of the web process. If you use it, the web process must not also fire routines, or every routine runs twice:

```bash
ODYSSEUS_INPROCESS_TASKS=0 ODYSSEUS_IMAGE=odysseus:fork \
  docker compose --profile background-worker up -d
```

## B. Native Python, or a hybrid

Odysseus can run from a virtualenv. Chroma and SearXNG are still separate programs. The practical hybrid is: let Compose run `chromadb`, `searxng`, and `ntfy`, and run the Python app yourself against the same `./data`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
```

`setup.py` is safe to re-run. It leaves an existing `auth.json` and `.env` alone.

Use the same database file the container used:

```bash
export DATABASE_URL=sqlite:///./data/app.db
export CHROMADB_HOST=localhost
export CHROMADB_PORT=8100
export SEARXNG_INSTANCE=http://127.0.0.1:8080
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

Compose publishes Chroma on host port 8100 (the container listens on 8000). SearXNG is on host port 8080. If you are not using Compose for those, start them yourself and point the variables at whatever ports you chose.

Migrations run when the app imports the database layer, so the first launch of this fork updates `data/app.db` in place.

Stop the `odysseus` container before this native process starts. Two writers on one SQLite file will corrupt it.

This route is worth it when you are changing Python often. For day-to-day use of a personal assistant, the Docker route above is simpler.

## C. A background model that can run beside chat

With one local model and one slot, a routine used to wait forever while a browser tab was open, and it was cancelled if you started chatting mid-run. The default now is `defer`: the routine waits for a quiet moment, but at most `BACKGROUND_TASK_DEFER_MAX_WAIT_SECONDS` (180 seconds), then runs anyway. It is not cancelled when you are chatting.

If the model can do two things at once, routines start immediately. Any of these turns the default `auto` policy into `run`:

- `BACKGROUND_MODEL_ENDPOINT` — a second base URL used for background work (another llama.cpp port, or another Ollama model).
- `BACKGROUND_MODEL_SLOTS` greater than 1 — how many background calls may use the same endpoint together.
- `BACKGROUND_MODEL_PARALLEL=1` — you already started the server with extra slots and just need Odysseus to overlap chat and routines.
- `BACKGROUND_ENDPOINT_SLOTS` — a per-URL map, for example `http://127.0.0.1:8081=2,http://127.0.0.1:8082=1`.

llama.cpp:

```bash
llama-server -m your-model.gguf --port 8081 --parallel 2
```

Then in the Odysseus environment (`.env` next to Compose, or the shell for native):

```bash
BACKGROUND_MODEL_ENDPOINT=http://127.0.0.1:8081
BACKGROUND_MODEL_SLOTS=2
```

From inside Docker, the host is `http://host.docker.internal:8081`, not `127.0.0.1`.

Ollama:

```bash
OLLAMA_NUM_PARALLEL=2 ollama serve
```

and `BACKGROUND_MODEL_PARALLEL=1` (or `BACKGROUND_MODEL_SLOTS=2`).

Per task, set `foreground_policy` to `run`, `defer`, or `yield`. `yield` is the old behaviour: postpone 15 minutes and cancel the run when you are active. Leave the field empty to follow the global policy.

Other settings:

| Setting | Meaning |
| --- | --- |
| `BACKGROUND_FOREGROUND_POLICY` | `auto` (default), `run`, `defer`, or `yield` |
| `BACKGROUND_TASK_DEFER_MAX_WAIT_SECONDS` | How long `defer` waits before running anyway. Default 180. `0` means do not wait. |
| `BACKGROUND_TASK_FOREGROUND_GATE` | `false` forces `run`, same as before. |
| `BACKGROUND_JOB_MAX_ATTEMPTS` | Retries for a failing non-schedule background job. Default 3. |
| `WEBHOOK_RETRY_DELAYS` | Seconds between outgoing webhook tries. Default `0,1,4`. Network errors, HTTP 429, and HTTP 5xx are retried. Other 4xx are not. |
| `ODYSSEUS_INPROCESS_TASKS` | `0` only when the optional `background-worker` profile is running. |

## D. Check that routines actually run

1. Set a model endpoint as above if you have a parallel server. Restart Odysseus so it reads the variables.
2. Leave a chat tab open. Create a task that runs every minute, or use Run now on an existing task.
3. Activity should show the run go to success. It should not sit on "Paused because Odysseus became active" and slide 15 minutes into the future over and over.
4. Open `GET /api/tasks/meta/background-jobs` (while logged in). A handed-off task, a parked approval, or a webhook retry shows up there with a short history. Restart Odysseus and refresh: a queued handoff is still listed and is picked up again.
5. Ask the assistant to hand a long task to the background (`handoff_background`). The reply should include a job id, and the result should appear in that same chat when it finishes, including after a restart if it had not finished yet.
6. For mail: the first time a mailbox is seen, existing messages are only remembered. A later poll that sees a new inbox message fires `email_received` with subject, sender, and id. A task prompt can use `{{event.subject}}` and `{{event.from}}`. Those fields are data, not instructions.
7. A background task that wants to act on untrusted mail or web content parks and notifies you. Nothing from that step runs until you approve it. After a restart the parked action expires and does not run, because the one-time grant lived only in memory.

## E. Phase 2: model routing and Outlook

Phase 2 is documented in [PHASE2_SETUP.md](PHASE2_SETUP.md). It adds a local-first model route (a 9B model for easy work, an API model when the work is hard or the local call fails) and Outlook / Microsoft 365 mail, calendar, and contacts. Apply it on top of this branch. The same `./data` folder stays in place.
