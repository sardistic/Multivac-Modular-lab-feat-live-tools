"""Bounded, owner-invoked patch generation for code proposals."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import PurePosixPath

from providers.openai_client import OPENAI_CHAT_MODEL
from providers.openai_messages import generate_openai_messages_response_with_tools
from services.code_changes import MAX_PATCH_BYTES, REPO_PATH, inspect_patch, is_protected_path

MAX_CONTEXT_CHARS = 70_000
MAX_FILES = 14
CODE_MODEL = os.getenv("OPENAI_CODE_MODEL", OPENAI_CHAT_MODEL)


def _git_at(baseline: str, *args: str, max_chars: int = 100_000) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_PATH, capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Git context lookup failed").strip()[:500])
    return result.stdout[:max_chars]


def _request_terms(request: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z][a-z0-9_]{2,}", request.lower())
        if token not in {"the", "and", "that", "with", "from", "this", "make", "change", "code", "bot"}
    }


def select_code_context(request: str, baseline: str) -> list[tuple[str, str]]:
    tracked = _git_at(baseline, "ls-tree", "-r", "--name-only", baseline).splitlines()
    candidates = [
        path for path in tracked
        if PurePosixPath(path).suffix in {".py", ".md", ".toml", ".yml", ".yaml"}
        and not is_protected_path(path)
        and not path.startswith(("archive/", "dev/"))
    ]
    terms = _request_terms(request)
    ranked: list[tuple[int, str, str]] = []
    for path in candidates:
        try:
            content = _git_at(baseline, "show", f"{baseline}:{path}", max_chars=25_000)
        except RuntimeError:
            continue
        low = f"{path}\n{content}".lower()
        score = sum((8 if term in path.lower() else 1) * low.count(term) for term in terms)
        if path in {"readme.md", "discord_bot.py"}:
            score += 2
        ranked.append((score, path, content))
    ranked.sort(key=lambda item: (-item[0], len(item[2]), item[1]))

    selected: list[tuple[str, str]] = []
    used = 0
    for _score, path, content in ranked:
        block_size = len(path) + len(content)
        if selected and (len(selected) >= MAX_FILES or used + block_size > MAX_CONTEXT_CHARS):
            continue
        selected.append((path, content))
        used += block_size
        if len(selected) >= MAX_FILES:
            break
    return selected


def extract_unified_diff(text: str) -> str:
    text = (text or "").strip()
    fenced = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("diff --git ")
    if start < 0:
        raise ValueError("Model did not return a unified Git diff")
    patch = text[start:].strip() + "\n"
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError("Generated patch exceeds the patch-size limit")
    report = inspect_patch(patch)
    if not report["ok"]:
        raise ValueError("Generated patch violated policy: " + "; ".join(report["errors"][:4]))
    return patch


async def generate_code_patch(request: str, baseline: str) -> tuple[str, dict]:
    context = select_code_context(request, baseline)
    if not context:
        raise RuntimeError("No policy-allowed source context was found")
    source = "\n\n".join(
        f"===== FILE: {path} =====\n{content}" for path, content in context
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are generating a minimal code change for a reviewed deployment pipeline. "
                "Return ONLY one valid unified Git diff beginning with 'diff --git'. Do not use "
                "markdown fences or commentary. Change only files supplied below. Never modify "
                "tests, ops, secrets, configuration, dependency manifests, startup files, audit "
                "storage, or self-modification policy. Do not add dependencies. Preserve existing "
                "behavior outside the request."
            ),
        },
        {
            "role": "user",
            "content": (
                f"BASELINE SHA: {baseline}\nREQUEST: {request}\n\n"
                f"AVAILABLE SOURCE FILES:\n{source}"
            ),
        },
    ]
    response = await generate_openai_messages_response_with_tools(
        messages,
        tools=[],
        model=CODE_MODEL,
        max_tokens=8000,
        temperature=0.1,
    )
    if response.startswith("⚠️ OpenAI"):
        raise RuntimeError(response)
    patch = extract_unified_diff(response)
    return patch, {
        "model": CODE_MODEL,
        "context_files": [path for path, _ in context],
        "context_chars": sum(len(content) for _, content in context),
    }
