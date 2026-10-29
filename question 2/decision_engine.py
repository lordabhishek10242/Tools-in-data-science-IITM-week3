"""One batched, OpenAI-compatible AI decision request plus strict validation."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

ACTION_IDS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}


def semantic_package(package: dict[str, Any]) -> dict[str, Any]:
    """Remove batch-local identity while retaining the complete invoice evidence."""
    return {k: v for k, v in package.items() if k not in {"packageId", "package_id"}}


def package_key(package: dict[str, Any]) -> str:
    canonical = json.dumps(semantic_package(package), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_model_content(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError("Model did not return a JSON object")
        return json.loads(match.group(0))


def validate_decisions(packages: list[dict[str, Any]], value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("proposals", value.get("decisions"))
    if not isinstance(value, list) or len(value) != len(packages):
        raise ValueError("Model must return one proposal per package")
    expected = {str(p.get("packageId", p.get("package_id", ""))): p for p in packages}
    seen: set[str] = set()
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Proposal must be an object")
        package_id = str(item.get("packageId", ""))
        if not package_id or package_id not in expected or package_id in seen:
            raise ValueError("Package IDs must match the submitted batch exactly")
        seen.add(package_id)
        action = item.get("action")
        if action not in ACTION_IDS:
            raise ValueError("Unknown action")
        facts = item.get("facts")
        if not isinstance(facts, dict):
            raise ValueError("Facts must be an object")
        vendor, number, amount, currency = (facts.get("vendorName"), facts.get("invoiceNumber"),
                                            facts.get("amountMinor"), facts.get("currency"))
        if not isinstance(vendor, str) or not vendor.strip() or not isinstance(number, str) or not number.strip():
            raise ValueError("Invoice vendor and number are required")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("amountMinor must be a nonnegative integer")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter uppercase code")
        refs = item.get("evidenceRefs")
        if not isinstance(refs, list) or len(refs) != 3 or any(
            not isinstance(ref, str) or len(ref) < 3 or not (ref.startswith("[") and ref.endswith("]"))
            for ref in refs
        ) or len(set(refs)) != 3:
            raise ValueError("Exactly three unique bracketed evidence references are required")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not 60 <= len(rationale) <= 1500:
            raise ValueError("Rationale length is outside the required range")
        if action.replace("_", " ") not in rationale.lower().replace("_", " "):
            raise ValueError("Rationale must name the chosen action")
        if sum(ref in rationale for ref in refs) < 2:
            raise ValueError("Rationale must cite at least two evidence references")
        cleaned.append({
            "packageId": package_id,
            "action": action,
            "facts": {"vendorName": vendor.strip(), "invoiceNumber": number.strip(),
                      "amountMinor": amount, "currency": currency},
            "evidenceRefs": refs,
            "rationale": rationale,
        })
    if seen != set(expected):
        raise ValueError("Proposal package coverage mismatch")
    return cleaned


async def decide_batch(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = os.getenv("AI_BASE_URL", "").rstrip("/")
    model = os.getenv("AI_MODEL", "")
    api_key = os.getenv("AI_API_KEY", "")
    if not base or not model:
        raise RuntimeError("AI_BASE_URL and AI_MODEL must be configured")
    if not api_key and "localhost" not in base and "127.0.0.1" not in base:
        raise RuntimeError("AI_API_KEY is required for hosted endpoints")
    system = """You are an evidence-based invoice action analyst. You only propose actions; never execute payments.
For every submitted package, select exactly one action: settle_invoice, request_approval, hold_invoice, reject_duplicate, open_exception.
Read the current operative paragraph and distinguish it from cover sheets, archived examples, training decoys, negations and irrelevant action words. A duplicate means this same commercial invoice was already paid; a different invoice from the same vendor is not a duplicate. Use the amount in minor currency units. Do not invent facts or references.
Return one JSON object: {"proposals":[{"packageId":"...","action":"...","facts":{"vendorName":"...","invoiceNumber":"...","amountMinor":0,"currency":"INR"},"evidenceRefs":["[exact ref]","[exact ref]","[exact ref]"],"rationale":"60-1500 characters; name action and cite at least two exact refs"}]}. Evidence references must be the exactly three decisive bracketed references from the paragraph that determines the action. Keep their brackets and exact text. Choose all actions only from the five allowed values."""
    payload = {"model": model, "temperature": 0, "max_tokens": 9000,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps({"packages": packages}, ensure_ascii=False)}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(38.0, connect=8.0)) as client:
        response = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return validate_decisions(packages, _parse_model_content(content))

