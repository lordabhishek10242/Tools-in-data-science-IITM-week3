# Question 2: Invoice action agent

An A2A 1.0 HTTP+JSON agent that reads invoice batches, uses one batched AI call for uncached packages, proposes one evidence-cited business action per package, and records executions only after matching grader receipts arrive.

## Safety and task state

- Proposals are the only result of an initial batch. The agent never pays, settles, or executes from a proposal alone.
- A continuation must match the stored principal, task, context, batch, package, action ID, action, outcome, and receipt nonce shape. Only `ACCEPTED` results appear in the receipt artifact.
- Tasks, message hashes, and semantic package decisions are stored in SQLite locally or PostgreSQL when `DATABASE_URL` is set. Bearer credentials are hashed before storage; each distinct token has a separate task namespace.
- Same-principal/same-message retries return the stored Task. Reusing an ID with changed message content returns `IDEMPOTENCY_CONFLICT`.
- Run one application worker. The operation lock serializes idempotency and the cancel-versus-result race; the database transaction stores terminal state and its message record together.

## Local run

Python 3.11+ is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:AI_BASE_URL = "<your provider's OpenAI-compatible base URL>"
$env:AI_MODEL = "<your provider's model name>"
$env:AI_API_KEY = "<your provider key>"
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

The agent expects an OpenAI-compatible chat-completions endpoint. For local Ollama, set `AI_BASE_URL=http://127.0.0.1:11434/v1`, choose an installed chat model, and leave `AI_API_KEY` empty. Do not commit provider keys.

## Deploy with Render Blueprint

1. In Render, create a Blueprint from this repository and select `question 2/render.yaml` as the Blueprint file.
2. During initial setup, provide the selected provider's base URL, model name and API key for the three prompted `AI_*` environment variables. Never place the key in source control.
3. The Agent Card is at `https://<service>.onrender.com/.well-known/agent-card.json`; the service derives its base URL from Render's documented `RENDER_EXTERNAL_URL` variable. Submit the `supportedInterfaces[0].url` value as the base URL.

The included Render Blueprint uses a free PostgreSQL database for durable state during short-lived verification. Render documents that free Postgres databases expire 30 days after creation; switch `DATABASE_URL` to a durable managed PostgreSQL database for longer-lived use. The web service uses a free instance, which can spin down when idle, so the first request after inactivity can be slow.

## A2A routes

- `GET /.well-known/agent-card.json` (public discovery)
- `POST /a2a/message:send`
- `GET /a2a/tasks/{id}`
- `GET /a2a/tasks`
- `POST /a2a/tasks/{id}:cancel`

For protected routes send `Authorization: Bearer <token>` and `A2A-Version: 1.0`. For POST requests send `Content-Type: application/a2a+json`. Initial `message.parts` contains a `application/vnd.ga5.invoice-claim-batch+json` data part; result continuations contain `application/vnd.ga5.invoice-action-results+json`.

