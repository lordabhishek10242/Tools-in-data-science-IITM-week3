"""Public A2A 1.0 HTTP+JSON invoice action agent."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse

from decision_engine import decide_batch, package_key
from store import Store, digest

CARD_PATH = "/.well-known/agent-card.json"
A2A_MEDIA = "application/a2a+json"
BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"
OUTPUT_MODES = [PROPOSALS_MEDIA, RECEIPTS_MEDIA]
BASE_PATH = os.getenv("A2A_BASE_PATH", "/a2a").rstrip("/") or "/a2a"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or f"{os.getenv('RENDER_EXTERNAL_URL', 'https://localhost').rstrip('/')}{BASE_PATH}/"
if not PUBLIC_BASE_URL.endswith("/"):
    PUBLIC_BASE_URL += "/"

app = FastAPI(title="Public Invoice Action Agent", docs_url=None, redoc_url=None, openapi_url=None)
store = Store()
# One worker is used in the container. The lock makes duplicate submissions and
# the cancel/result race linearizable within that process; SQLite commits persist state.
operation_lock = asyncio.Lock()


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status, media_type=A2A_MEDIA)


@app.exception_handler(Exception)
async def generic_error_handler(_request: Request, _exc: Exception):
    return JSONResponse({"error": {"code": "INTERNAL_ERROR"}}, status_code=500, media_type=A2A_MEDIA)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException):
    code = "NOT_FOUND" if exc.status_code == 404 else "METHOD_NOT_ALLOWED" if exc.status_code == 405 else "REQUEST_ERROR"
    return JSONResponse({"error": {"code": code}}, status_code=exc.status_code, media_type=A2A_MEDIA)


def new_id() -> str:
    return str(uuid.uuid4())


def principal_for(request: Request) -> str:
    value = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer ([^\s]+)", value)
    if not match:
        raise ApiError(401, "UNAUTHORIZED")
    return hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()


def require_protocol(request: Request, principal: bool = True, content_type: bool = False):
    owner = principal_for(request) if principal else None
    if request.headers.get("a2a-version") != "1.0":
        raise ApiError(400, "UNSUPPORTED_VERSION")
    if content_type and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != A2A_MEDIA:
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE")
    return owner


def output(data: Any, status: int = 200):
    return JSONResponse(data, status_code=status, media_type=A2A_MEDIA)


def get_part(message: dict, media_type: str):
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise ApiError(400, "INVALID_MESSAGE")
    hits = [p for p in parts if isinstance(p, dict) and p.get("mediaType") == media_type and isinstance(p.get("data"), dict)]
    if len(hits) != 1:
        raise ApiError(400, "INVALID_MESSAGE")
    return hits[0]["data"]


def validate_message(envelope: Any):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("message"), dict):
        raise ApiError(400, "INVALID_MESSAGE")
    message = envelope["message"]
    if message.get("role") != "ROLE_USER" or not isinstance(message.get("messageId"), str) or not message["messageId"]:
        raise ApiError(400, "INVALID_MESSAGE")
    config = envelope.get("configuration")
    if not isinstance(config, dict):
        raise ApiError(400, "INVALID_CONFIGURATION")
    modes = config.get("acceptedOutputModes")
    if not isinstance(modes, list) or any(mode not in modes for mode in OUTPUT_MODES):
        raise ApiError(400, "UNSUPPORTED_OUTPUT_MODE")
    return message, config


def public_task(task: dict):
    return copy.deepcopy(task)


@app.get(CARD_PATH)
async def agent_card():
    return output({
        "name": "Public HTTPS Invoice Action Agent",
        "description": "Reviews invoice packages, proposes evidence-cited business actions, and records accepted action receipts safely.",
        "version": "1.0.0",
        "supportedInterfaces": [{"url": PUBLIC_BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": [A2A_MEDIA, BATCH_MEDIA, RESULTS_MEDIA],
        "defaultOutputModes": OUTPUT_MODES,
        "skills": [{
            "id": "invoice_action_agent",
            "name": "Invoice action review",
            "description": "Reviews invoice packages and proposes one evidence-based action per invoice, then records accepted receipts.",
            "tags": ["invoice", "evidence", "approval", "duplicate-payment"],
            "inputModes": [BATCH_MEDIA, RESULTS_MEDIA],
            "outputModes": OUTPUT_MODES,
        }],
    })


@app.post(f"{BASE_PATH}/message:send")
async def send_message(request: Request):
    owner = require_protocol(request, content_type=True)
    try:
        envelope = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_MESSAGE")
    message, config = validate_message(envelope)
    message_hash = digest(message)  # Configuration is intentionally excluded.
    message_id = message["messageId"]

    async with operation_lock:
        previous = store.get_message(owner, message_id)
        if previous:
            if previous["semantic_hash"] != message_hash:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT")
            task = store.get_task(owner, previous["task_id"])
            return output({"task": public_task(task)})

        if message.get("taskId") is not None:
            task = await continue_task(owner, message, message_hash, message_id)
            return output({"task": public_task(task)})

        data = get_part(message, BATCH_MEDIA)
        batch_id = data.get("batchId")
        if not isinstance(data.get("policyRevision"), str) or not data["policyRevision"]:
            raise ApiError(400, "INVALID_BATCH")
        packages = data.get("packages")
        if not isinstance(batch_id, str) or not batch_id or not isinstance(packages, list) or not packages:
            raise ApiError(400, "INVALID_BATCH")
        package_ids = [p.get("packageId", p.get("package_id")) if isinstance(p, dict) else None for p in packages]
        if any(not isinstance(pid, str) or not pid for pid in package_ids) or len(set(package_ids)) != len(package_ids):
            raise ApiError(400, "INVALID_BATCH")

        keys = [package_key(p) for p in packages]
        cached = [store.get_decision(key) for key in keys]
        missing_indices = [i for i, decision in enumerate(cached) if decision is None]
        if missing_indices:
            try:
                fresh = await decide_batch([packages[i] for i in missing_indices])
            except Exception:
                raise ApiError(502, "DECISION_FAILED")
            for index, decision in zip(missing_indices, fresh, strict=True):
                cache_value = {k: decision[k] for k in ("action", "facts", "evidenceRefs", "rationale")}
                store.save_decision(keys[index], cache_value)
                cached[index] = cache_value

        proposals = []
        action_ids: set[str] = set()
        for package, decision in zip(packages, cached, strict=True):
            action_id = new_id().replace("-", "")
            while action_id in action_ids:
                action_id = new_id().replace("-", "")
            action_ids.add(action_id)
            proposals.append({"packageId": package.get("packageId", package.get("package_id")),
                              "actionId": action_id, "action": decision["action"],
                              "facts": decision["facts"], "evidenceRefs": decision["evidenceRefs"],
                              "rationale": decision["rationale"]})

        context_id, task_id = new_id(), new_id()
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
            "history": [copy.deepcopy(message)],
            "artifacts": [{"artifactId": new_id(), "parts": [{"mediaType": PROPOSALS_MEDIA,
                                                                  "data": {"batchId": batch_id, "proposals": proposals}}]}],
        }
        store.save_task(task, owner, batch_id, message_id, message_hash)
        return output({"task": task})


async def continue_task(owner: str, message: dict, message_hash: str, message_id: str):
    task_id = message.get("taskId")
    stored = store.get_task(owner, task_id) if isinstance(task_id, str) else None
    if stored is None:
        # Same generic response whether the task is missing or owned by someone else.
        raise ApiError(404, "TASK_NOT_FOUND")
    if message.get("contextId") != stored.get("contextId"):
        raise ApiError(409, "TASK_CONTEXT_MISMATCH")
    if stored["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
        raise ApiError(409, "TASK_TERMINAL")
    data = get_part(message, RESULTS_MEDIA)
    proposal_data = stored["artifacts"][0]["parts"][0]["data"]
    if data.get("batchId") != proposal_data.get("batchId"):
        raise ApiError(409, "BATCH_MISMATCH")
    proposals = proposal_data["proposals"]
    results = data.get("results")
    if not isinstance(results, list) or len(results) != len(proposals):
        raise ApiError(400, "INVALID_RESULTS")
    by_package = {p["packageId"]: p for p in proposals}
    seen = set()
    accepted = []
    for result in results:
        if not isinstance(result, dict):
            raise ApiError(400, "INVALID_RESULTS")
        pid = result.get("packageId")
        proposal = by_package.get(pid)
        if proposal is None or pid in seen or result.get("actionId") != proposal["actionId"] or result.get("action") != proposal["action"]:
            raise ApiError(409, "PROPOSAL_MISMATCH")
        seen.add(pid)
        if result.get("outcome") not in {"ACCEPTED", "REJECTED"} or not isinstance(result.get("receiptNonce"), str) or not result["receiptNonce"]:
            raise ApiError(400, "INVALID_RESULTS")
        if result["outcome"] == "ACCEPTED":
            accepted.append({"packageId": pid, "actionId": proposal["actionId"], "action": proposal["action"],
                             "receiptNonce": result["receiptNonce"], "facts": proposal["facts"],
                             "evidenceRefs": proposal["evidenceRefs"]})
    if seen != set(by_package):
        raise ApiError(400, "INVALID_RESULTS")

    updated = copy.deepcopy(stored)
    updated["history"].append(copy.deepcopy(message))
    updated["artifacts"].append({"artifactId": new_id(), "parts": [{"mediaType": RECEIPTS_MEDIA,
        "data": {"batchId": data["batchId"], "executions": accepted}}]})
    updated["status"] = {"state": "TASK_STATE_COMPLETED"}
    store.save_task(updated, owner, data["batchId"], message_id, message_hash)
    return updated


@app.get(f"{BASE_PATH}/tasks")
async def list_tasks(request: Request):
    owner = require_protocol(request)
    return output({"tasks": store.list_tasks(owner)})


@app.get(f"{BASE_PATH}/tasks/{{task_id}}")
async def read_task(task_id: str, request: Request):
    owner = require_protocol(request)
    task = store.get_task(owner, task_id)
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND")
    return output(public_task(task))


@app.post(f"{BASE_PATH}/tasks/{{task_id}}:cancel")
async def cancel_task(task_id: str, request: Request):
    owner = require_protocol(request)
    async with operation_lock:
        task = store.get_task(owner, task_id)
        if task is None:
            raise ApiError(404, "TASK_NOT_FOUND")
        if task["status"]["state"] in {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"}:
            raise ApiError(409, "TASK_TERMINAL")
        task["status"] = {"state": "TASK_STATE_CANCELED"}
        store.save_task(task, owner, task["artifacts"][0]["parts"][0]["data"]["batchId"])
        return output(public_task(task))

