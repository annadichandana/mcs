"""One entry point for structured Claude calls: Anthropic API (key) or the Claude Code CLI (`claude -p`, uses your Claude login)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile

from .config import settings


class LLMError(Exception):
    pass


def cli_path() -> str | None:
    return settings.claude_cli_path or shutil.which("claude")


def backend() -> str:
    mode = settings.llm_backend.lower()
    if mode in ("api", "cli"):
        return mode
    if settings.anthropic_api_key:
        return "api"
    return "cli" if cli_path() else "none"


def available() -> bool:
    b = backend()
    return (b == "api" and bool(settings.anthropic_api_key)) or (b == "cli" and bool(cli_path()))


def _extract_json(text: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


def _cli_result_text(stdout: str) -> str:
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(obj, dict):
        if obj.get("is_error"):
            raise LLMError(f"Claude CLI error: {obj.get('result')}")
        return obj.get("result") or ""
    if isinstance(obj, list):  # some versions emit a list of events
        for ev in reversed(obj):
            if isinstance(ev, dict) and ev.get("type") == "result":
                return ev.get("result") or ""
    return stdout


def _cli(system: str, user: str, tool: dict) -> dict:
    schema = tool["input_schema"]
    key = schema["required"][0]
    prompt = (
        f"{system}\n\n---\nRespond with ONLY one JSON object that validates against the JSON Schema below. "
        f"No commentary, no markdown fences, and do not use any tools.\nSchema:\n{json.dumps(schema)}\n\n---\n{user}"
    )
    cmd = [cli_path(), "-p", "--output-format", "json"]
    if settings.claude_cli_model:
        cmd += ["--model", settings.claude_cli_model]
    for attempt in range(2):
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               timeout=900, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            raise LLMError("The claude CLI timed out after 15 minutes.")
        if p.returncode != 0:
            raise LLMError(f"claude CLI failed ({p.returncode}): {(p.stderr or p.stdout or '')[:600]}")
        data = _extract_json(_cli_result_text(p.stdout))
        if isinstance(data, dict) and key in data:
            return data
        prompt += f'\n\n(Your previous reply was not a valid JSON object with a "{key}" field. Reply with only that JSON.)'
    raise LLMError("The claude CLI did not return valid JSON after 2 attempts.")


def structured(client, system: str, user: str, tool: dict, max_tokens: int) -> dict:
    """client is an Anthropic client (API path) or None (CLI path). Returns the tool's input dict."""
    if client is None:
        return _cli(system, user, tool)
    resp = client.messages.create(
        model=settings.model, max_tokens=max_tokens, system=system, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "max_tokens":
        raise LLMError("Claude's response was truncated; lower CHUNK_CHARS and retry.")
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return dict(block.input)
    raise LLMError("Claude returned no structured output.")
