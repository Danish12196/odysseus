# Phase 2: local-first models and Outlook

This sits on the Phase 1 branch (`cursor/reliable-background-routines-b8be`, pull request #1). That branch is not merged. Check this fork out on top of it, or merge Phase 1 first. Your existing `./data` folder stays. Do not delete it.

Phase 2 does two things:

- Easy chat, utility calls, and background routines stay on a local model such as Qwen 3.5 9B. A long request, a request you mark as hard, or a local call that fails moves to an API model.
- Outlook and Microsoft 365 mail uses Microsoft sign-in on the same IMAP/SMTP path as Gmail, including the background poller and the Phase 1 `email_received` event. Calendar and contacts are an optional Graph connection.

Odysseus already talks to Ollama, llama.cpp, xAI, Anthropic, and OpenAI through saved model endpoints. This does not replace that. A LiteLLM proxy is optional and off unless you start its Compose profile.

## 1. Put this on the Docker install you already have

From the directory that has `docker-compose.yml` and `./data`:

```bash
docker compose stop odysseus
```

Point the checkout at this branch. Leave `./data` where it is. Add the variables in the next sections to the `.env` file next to Compose (create it if you do not have one). Compose uses `ghcr.io/odysseus-dev/odysseus:latest` whenever `ODYSSEUS_IMAGE` is unset or empty, so set it to the local tag on both the build and every start:

```bash
ODYSSEUS_IMAGE=odysseus:fork docker compose build odysseus
ODYSSEUS_IMAGE=odysseus:fork docker compose up -d
```

Keep that same value for later `up -d` commands, including the optional profile commands below. An empty value pulls the upstream image and you will not get this fork.

`docker compose up` does not start the Microsoft Graph container or LiteLLM. Those are separate profiles, below.

The app listens on port 7000 inside the container. Compose publishes that as host port **7011** unless you set `APP_PORT`. Browser links and OAuth redirects must use the host port.

## 2. Local model plus one API model

From inside Docker, Ollama on the host is `host.docker.internal`, not `127.0.0.1`. Start Ollama so it listens on all interfaces (`OLLAMA_HOST=0.0.0.0:11434`).

`.env` for a local Qwen and xAI when the work is hard:

```bash
LOCAL_MODEL_BASE_URL=http://host.docker.internal:11434/v1
LOCAL_MODEL_NAME=qwen3.5:9b
XAI_API_KEY=your-xai-key
XAI_MODEL=grok-4
OAUTH_REDIRECT_BASE_URL=http://localhost:7011
```

Anthropic instead of xAI (or as well — xAI is used first when both keys are set):

```bash
ANTHROPIC_API_KEY=your-anthropic-key
ANTHROPIC_MODEL=claude-sonnet-4-5
```

OpenAI is not picked up from `OPENAI_API_KEY` alone, because that variable is already used for other features. Either set the escalation URL yourself, or turn routing on explicitly:

```bash
WORK_ROUTING_ENABLED=1
OPENAI_API_KEY=your-openai-key
OPENAI_MODEL=gpt-4.1
```

Or point the escalation slot at any OpenAI-compatible server, including a private one:

```bash
ESCALATION_MODEL_BASE_URL=https://api.openai.com/v1
ESCALATION_MODEL_NAME=gpt-4.1
ESCALATION_MODEL_API_KEY=your-openai-key
```

`ESCALATION_MODEL_*` wins over the provider keys. The key can be omitted when it is the same as `XAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.

llama.cpp on the host is the same idea:

```bash
LOCAL_MODEL_BASE_URL=http://host.docker.internal:8081/v1
LOCAL_MODEL_NAME=qwen3.5-9b
```

Leave `LOCAL_MODEL_*` and the API variables unset if you already chose endpoints in Settings and want those left alone. Routing turns on by itself when a local URL or an xAI/Anthropic key is present. `WORK_ROUTING_ENABLED=0` forces the old behaviour.

On startup, empty default, utility, and background model settings are filled from `LOCAL_MODEL_*`. Settings you already saved are not overwritten. Rows named `work-routing-local` and `work-routing-escalation` are rewritten from the environment so the Settings screen matches `.env`.

### What goes where

| Kind of work | First model | If that call fails |
| --- | --- | --- |
| Chat, utility, background routines | Local, unless the turn is hard | The escalation model |
| Hard turn | Escalation model | Local, if the API call fails |
| Vision | `VISION_MODEL_*` when set, otherwise the escalation model | The model Settings already had for vision |
| Coding | `CODING_MODEL_*` when that kind of call is used, or when a chat is both long and looks like code | Local, then the general escalation model |

A turn is hard when any of these is true:

- The text contains `[[escalate]]`, `use the stronger model`, or `escalate this`.
- The latest user message is at least `WORK_ROUTING_ESCALATE_CHARS` characters (default 6000).
- The turn already has `WORK_ROUTING_ESCALATE_ROUNDS` tool results (default 4).

A turn that is already aimed at an API model is not reordered. If you pick Grok in the chat box, that choice stays.

`WORK_ROUTING_AUTO_ESCALATE=0` stops the teacher takeover described next. Failure fallback (try the next model in the list) still runs.

Optional vision and coding slots:

```bash
VISION_MODEL_BASE_URL=http://host.docker.internal:11434/v1
VISION_MODEL_NAME=qwen3.5:9b
CODING_MODEL_BASE_URL=https://api.x.ai/v1
CODING_MODEL_NAME=grok-4
CODING_MODEL_API_KEY=your-xai-key
```

If `vision_model` in Settings is empty and an escalation model exists, startup points vision at that escalation model. A local Qwen that can see images should set `VISION_MODEL_*` so pictures stay on the machine.

### Automatic escalation

Two mechanisms, both already in Odysseus, now share this route:

1. **Fallback.** Chat, background tasks, and vision walk the candidate list. A failed local call is retried on the API model. HTTP 408, 425, 429, 500, 502, 503, 504, 507, 508, and 529 already count as retryable for streaming chat.
2. **Teacher takeover.** When auto-escalate is on, a reply that gives up (“I can't do this”, a tool error, and the same patterns the teacher already used) or a turn that crosses the complexity limits is handed to the escalation model inside the same chat. You see a takeover banner. If Settings already has `teacher_enabled` and `teacher_model`, those still win over the environment.

Background routines use the same candidate list, so a routine that the 9B model cannot finish is retried on the API model. Phase 1 still decides *when* a routine runs (`defer` / `run` / `yield`). This only decides *which model* runs it.

### Cost

Each chat already records `total_cost_usd` from `src/model_pricing.py`. Known prices include `grok-4` and `claude-sonnet-4-5`. Local hosts (`localhost`, `127.0.0.1`, `host.docker.internal`, private IPs) are treated as free.

A daily cap is optional:

```bash
MODEL_API_DAILY_BUDGET_USD=2
MODEL_API_CALL_ESTIMATE_USD=0.02
```

`0` or unset means no cap. The ledger is `data/model_api_budget.json` (or `MODEL_API_BUDGET_PATH`) and resets at UTC midnight. Each successful API call adds a conservative estimate: 2,000 input and 500 output tokens at the known price, otherwise `MODEL_API_CALL_ESTIMATE_USD`. Once the cap is reached, later API candidates are skipped and work stays local until the next UTC day.

The cap is an estimate, not the provider's invoice. Parallel calls can pass the cap by about one estimate. A private proxy URL is not added to the ledger (it does not look like a public API host) but an escalation route is still skipped once the cap is already spent, because escalation is marked as an API call. Streaming counts one estimate per successful candidate, not the exact token bill.

### Check routing

Restart after editing `.env`, then sign in and open runtime info. `/api/runtime` is behind the login cookie (`odysseus_session`), unlike `/api/health`:

```bash
curl -s -b 'odysseus_session=YOUR_SESSION_COOKIE' http://127.0.0.1:7011/api/runtime | python3 -m json.tool
```

Copy the cookie from the browser after you sign in (DevTools → Application → Cookies). A curl without it returns 401.

`work_routing.active` should be true. `work_routing.local_host` should be `host.docker.internal`. `work_routing.escalation_label` should be `xAI`, `Anthropic`, `OpenAI`, or `Escalation model`. The API key is not in this response.

In chat, a short question should hit the local model. A message that contains `[[escalate]]` should hit the API model first. Settings → Added Models should list the `work-routing-*` endpoints.

### LiteLLM, if you want one URL

Native clients are enough for one xAI, Anthropic, or OpenAI key. LiteLLM helps only when you want a single OpenAI-compatible URL that can swap those providers in one config file, or when another program on the machine should share the same proxy.

```bash
# .env
LITELLM_MASTER_KEY=choose-a-local-secret
XAI_API_KEY=your-xai-key
ESCALATION_MODEL_BASE_URL=http://litellm:4000/v1
ESCALATION_MODEL_NAME=grok
ESCALATION_MODEL_API_KEY=choose-a-local-secret

ODYSSEUS_IMAGE=odysseus:fork docker compose --profile litellm up -d
```

The sample config is `config/litellm/config.yaml` (`grok`, `claude`, `gpt`). Compose pins `ghcr.io/berriai/litellm:main-latest` to a digest so a later pull does not change the bits. Do not put LiteLLM on the public internet with an empty master key.

## 3. Outlook and Microsoft 365 mail

Mail uses Microsoft's Outlook scopes (`IMAP.AccessAsUser.All` and `SMTP.Send`), not Microsoft Graph. Graph is a different token and is only used for the optional calendar/contacts server. The same Entra app can hold both permission sets. Each sign-in asks for one set.

### Register the app (personal or work)

You need a Microsoft Entra app registration. A personal Microsoft account can create one at [https://entra.microsoft.com](https://entra.microsoft.com) (or [https://portal.azure.com](https://portal.azure.com) → Microsoft Entra ID). A work account should use the work tenant if you are allowed to register apps. If you are not, ask an admin to register it, or register it in a tenant you control and have them consent (next section).

1. Entra admin center → **Identity** → **Applications** → **App registrations** → **New registration**.
2. Name: `Odysseus`.
3. Supported account types:
   - **Personal Outlook.com only:** “Personal Microsoft accounts only”.
   - **One work tenant:** “Accounts in this organizational directory only”.
   - **Both:** “Accounts in any organizational directory and personal Microsoft accounts”. `common` only works for this third choice. As of June 2026, personal refresh tokens from the `common` endpoint are rejected, so a personal mailbox should use **consumers** even if the app allows both.
4. Redirect URI, platform **Web**:
   `http://localhost:7011/api/email/oauth/microsoft/callback`
   The host, port, and path must match exactly. Change the port if `APP_PORT` is not 7011. Add `https://your-domain/api/email/oauth/microsoft/callback` as a second URI if you use a reverse proxy, and set `MICROSOFT_OAUTH_REDIRECT_URI` to that same string.
5. After it is created, copy the **Application (client) ID**.
6. **Certificates & secrets** → **New client secret**. Copy the secret value immediately. Entra shows it only once.
7. **API permissions** → **Add a permission** → **APIs my organization uses** → search **Office 365 Exchange Online** → **Delegated**:
   - `IMAP.AccessAsUser.All`
   - `SMTP.Send`
   Also add Microsoft Graph delegated `offline_access`, `openid`, `profile`, `email`, and `User.Read` if they are not already on the list. Odysseus asks for `offline_access openid profile email` plus the two Outlook scopes. Do not add Graph `Mail.Send` to this sign-in. Graph mail scopes and Outlook scopes cannot be in the same token.
8. For the optional calendar/contacts server, add Graph delegated `Mail.Read`, `Calendars.ReadWrite`, `Contacts.Read`, and `User.Read` on the same app, but that server performs its own sign-in.

`.env`:

```bash
MICROSOFT_OAUTH_CLIENT_ID=your-application-id
MICROSOFT_OAUTH_CLIENT_SECRET=your-secret
MICROSOFT_OAUTH_TENANT=organizations
MICROSOFT_OAUTH_REDIRECT_URI=http://localhost:7011/api/email/oauth/microsoft/callback
OAUTH_REDIRECT_BASE_URL=http://localhost:7011
```

`MICROSOFT_OAUTH_TENANT` is the default when the connect button does not pass one. Use `consumers` for personal, `organizations` for work, a tenant GUID for one company, or `common` only when the registration allows both.

Restart Odysseus after changing `.env`.

### Connect the mailbox

1. Open `http://localhost:7011` and sign in.
2. Settings → Integrations → **Add Integration** → **Email (IMAP/SMTP)**.
3. Provider: **Outlook / Office 365**. Hosts fill in as `outlook.office365.com:993` and `smtp.office365.com:587` (STARTTLS). The password fields hide. **Connect with Microsoft** appears, with Work or school / Personal / both.
4. Account type: **Work or school**, or paste the tenant id. Personal: **Personal Outlook.com**.
5. Email: the mailbox address. You can leave username blank. The sign-in fills it in. If you type a username, it must be the same address you sign in with.
6. **Connect with Microsoft**. Sign in. You should land back on Integrations with a success banner.
7. Use **Test** on the account. IMAP should authenticate without a password.

Add a second mailbox as a second email account and connect it separately. Each account stores its own refresh token. A work mailbox and a personal mailbox can use different tenant choices.

The poller uses this token. The first poll only remembers messages already in the inbox. A later new message fires `email_received` with subject, sender, and id, same as Gmail. A task prompt can use `{{event.subject}}` and `{{event.from}}`. Those fields are data, not instructions.

**Send** still opens an approval. Approving it sends with SMTP and the Microsoft token. Nothing is sent because the model called a Graph tool.

Exchange Online must have IMAP and authenticated SMTP enabled for the mailbox (Microsoft 365 admin center → the user → Mail → Manage email apps). If those are off, sign-in succeeds and the connection test fails until an admin enables them.

### If the work tenant blocks user consent

You cannot bypass that. The mailbox stays disconnected until an admin grants consent.

1. Confirm the error on the Microsoft page (“Need admin approval” or similar).
2. Send the admin this link, with your client id, tenant id, and the same redirect URI registered on the app:

   ```text
   https://login.microsoftonline.com/YOUR-TENANT-ID/adminconsent?client_id=YOUR-CLIENT-ID&redirect_uri=http%3A%2F%2Flocalhost%3A7011%2Fapi%2Femail%2Foauth%2Fmicrosoft%2Fcallback
   ```

   Or, while logged into Odysseus, open:

   ```text
   http://localhost:7011/api/email/oauth/microsoft/authorize?account_id=ACCOUNT-ID&tenant=YOUR-TENANT-ID&admin_consent=1
   ```

   An admin of that tenant must open it. A successful admin consent returns to Odysseus with `email_oauth_success=admin_consent`. That does **not** connect your mailbox. It only allows the app in the tenant.
3. You then click **Connect with Microsoft** again and sign in as yourself.
4. If the admin will not consent, this app cannot read that mailbox. Use a tenant where you are allowed to consent, or ask them to register their own app and give you the client id and secret.

`MICROSOFT_OAUTH_CLIENT_SECRET` is a confidential client secret. Keep it in `.env`, not in chat.

### Check mail

```bash
curl -s -b 'odysseus_session=YOUR_SESSION_COOKIE' http://127.0.0.1:7011/api/runtime | python3 -m json.tool
```

`microsoft.oauth_configured` is true when both the client id and secret are set. It does not mean a mailbox is connected. The account list in Settings shows Microsoft as the provider and does not show the token.

A real Microsoft login, a tenant that blocks consent, and a live IMAP session were not part of the automated tests. Those need your tenant.

## 4. Calendar, contacts, and Graph reads

The Softeria `ms-365-mcp-server` image speaks Microsoft Graph. Odysseus already has an MCP client with OAuth, so the server is just another HTTP MCP row. It is optional. Mail does not need it.

```bash
MS365_MCP_CLIENT_ID=your-application-id
MS365_MCP_CLIENT_SECRET=your-secret
MS365_MCP_TENANT_ID=organizations
MS365_MCP_ORG_MODE=true
MS365_MCP_PRESET=outlook
MS365_MCP_URL=http://ms365-mcp:3000/mcp
MS365_MCP_PUBLIC_URL=http://127.0.0.1:3339
OAUTH_REDIRECT_BASE_URL=http://localhost:7011

ODYSSEUS_IMAGE=odysseus:fork docker compose --profile ms365 up -d
```

`outlook` is the small preset: mail, calendar, and contacts, plus tool discovery so a 9B model is not given the full Graph catalog. Odysseus also hides tools when the server still advertises more than `MS365_MCP_MAX_TOOLS` (default 24). What remains is list/get/search/read/download, drafts, and the discovery helpers.

Graph send, reply, forward, delete, and move stay hidden (`MS365_MCP_ALLOW_GRAPH_SEND` defaults to 0). The model is told to use `draft_email` or `send_email`, which go through approval and then the Outlook SMTP token. Creating a calendar event is not a mail send.

`--read-only` on the Softeria server also disables drafts, so the Compose default leaves that off and Odysseus blocks sends itself. Set `MS365_MCP_READ_ONLY=true` only if you want the server to reject every write, including drafts.

Several Graph logins are several MCP rows, because HTTP mode carries one login per Odysseus server:

```bash
MS365_MCP_SERVERS=work=http://ms365-mcp:3000/mcp,personal=http://ms365-home:3000/mcp
```

A second container is a copy of the `ms365-mcp` service with its own tenant and port. One container cannot hold two HTTP logins.

After `up`, Settings → Integrations should show `ms365` (or `ms365-work`) as an MCP row. Connect it and finish the Microsoft page. The redirect comes back to `OAUTH_REDIRECT_BASE_URL/api/mcp/oauth/callback`, which is why that variable must be `http://localhost:7011` and not the in-container port 7000.

`MS365_MCP_ORG_MODE=true` is for work tenants. Personal Microsoft accounts should use `MS365_MCP_TENANT_ID=consumers` and `MS365_MCP_ORG_MODE=false`.

Compose pins `ghcr.io/softeria/ms-365-mcp-server:0.173.0` with a digest. The published port is `127.0.0.1:3339`. Odysseus on the Compose network uses `http://ms365-mcp:3000/mcp`.

### Check Graph

`microsoft.mcp_servers` in `/api/runtime` lists `ms365` when `MS365_MCP_URL` is set. In chat, ask for today's calendar only after the MCP row shows connected. A send tool should be absent. If the model tries one anyway, the call returns an error that says to use the approval path, and no message is sent.

A live Graph login was not run in automation.

## 5. What to verify, in order

1. `curl -s http://127.0.0.1:7011/api/health` returns `"status": "healthy"`. Signed-in `/api/runtime` shows `work_routing` and `microsoft`. Without the session cookie that call is 401.
2. A short chat answers from the local model. `[[escalate]]` uses the API model. A failed local endpoint falls through (stop Ollama briefly if you want to see that).
3. With `MODEL_API_DAILY_BUDGET_USD` set very small, a couple of API calls then stay on the local model. The file `data/model_api_budget.json` shows today's UTC date and a spent figure. Delete the file to reset before midnight.
4. Outlook test connection succeeds after Microsoft sign-in. A new mail later produces `email_received`. A draft waits for approval before SMTP send.
5. With the `ms365` profile up and connected, a calendar question uses a handful of tools, and a Graph send tool is not offered.

`WORK_ROUTING_ENABLED=0` turns the new model order off without removing endpoints. Removing `MS365_MCP_URL` stops registering the Graph server on the next start. Existing mailbox tokens stay until you disconnect the email account.
