"""Shared Ark Responses transport. Role prompts and policy stay with the Agent."""
from __future__ import annotations
import json
import os
import urllib.request

DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"
DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/responses"


def configured_model():
    return os.getenv("ARK_MODEL", DEFAULT_MODEL)


def response_text(raw):
    if raw.get("error") or raw.get("status") in {"failed", "incomplete", "cancelled"}:
        raise ValueError("Ark returned a failed or incomplete model response")
    direct = raw.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = []
    for item in raw.get("output", []):
        if not isinstance(item, dict) or item.get("type") not in {None, "message"}:
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") in {None, "output_text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    result = "\n".join(parts).strip()
    if not result:
        raise ValueError("Ark returned no assistant text output")
    return result


def request_json(api_key, model, system, payload, max_tokens=3000, timeout=90, endpoint=None, content=None):
    user_content = content if content is not None else [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}]
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": user_content})
    body = {"model": model, "input": messages, "max_output_tokens": max_tokens,
            "thinking": {"type": "disabled"}, "text": {"format": {"type": "json_object"}}, "store": False}
    request = urllib.request.Request(endpoint or DEFAULT_ENDPOINT, data=json.dumps(body).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = json.loads(response.read().decode("utf-8"))
    text = response_text(raw)
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Ark must return a JSON object")
    return parsed
