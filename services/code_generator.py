"""Bounded, owner-invoked patch generation for code proposals."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import PurePosixPath

from providers.openai_client import OPENAI_CODE_MODEL
from providers.openai_messages import generate_openai_messages_response_with_tools
from services.code_changes import MAX_PATCH_BYTES, REPO_PATH, inspect_patch, is_protected_path

MAX_CONTEXT_CHARS = 70_000
MAX_FILES = 14
CODE_MODEL = OPENAI_CODE_MODEL


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


def _candidate_paths(baseline: str) -> list[str]:
    tracked = _git_at(baseline, "ls-tree", "-r", "--name-only", baseline).splitlines()
    return [
        path for path in tracked
        if PurePosixPath(path).suffix in {".py", ".md", ".toml", ".yml", ".yaml"}
        and not is_protected_path(path)
        and not path.startswith(("archive/", "dev/"))
    ]


def select_code_context(request: str, baseline: str) -> list[tuple[str, str]]:
    """Lexical fallback used only when model-driven repository planning fails."""
    candidates = _candidate_paths(baseline)
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


def _architecture_index(baseline: str, candidates: list[str]) -> str:
    """Build a compact, request-independent map of ownership and call-path clues."""
    blocks = []
    used = 0
    structural = re.compile(
        r"^\s*(?:class |def |async def |from |import )|"
        r"system|prompt|persona|personality|context|message|handler|dispatch|provider",
        re.IGNORECASE,
    )
    for path in candidates:
        try:
            content = _git_at(baseline, "show", f"{baseline}:{path}", max_chars=25_000)
        except RuntimeError:
            continue
        clues = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(content.splitlines(), 1)
            if structural.search(line)
        ][:35]
        block = f"[{path}]\n" + "\n".join(clues)
        if used + len(block) > 45_000:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


async def plan_code_context(request: str, baseline: str) -> tuple[list[tuple[str, str]], dict]:
    candidates = _candidate_paths(baseline)
    repo_map = "\n".join(candidates)
    architecture = _architecture_index(baseline, candidates)
    messages = [
        {
            "role": "system",
            "content": (
                "You are planning repository scope for a code change. Reason about architecture, "
                "ownership, call paths, and the user's intended breadth; do not select files merely "
                "because they repeat request words. Distinguish a global behavior owner from a local "
                "handler that happens to contain similar prose. Return only JSON with keys files "
                "(an ordered array of 1-14 exact paths), rationale, and scope_check. Include the source "
                "owners and their important consumers when the request is cross-cutting."
            ),
        },
        {
            "role": "user",
            "content": (
                f"REQUEST: {request}\nBASELINE: {baseline}\n\n"
                f"ALLOWED REPOSITORY MAP:\n{repo_map}\n\n"
                f"ARCHITECTURE INDEX:\n{architecture}"
            ),
        },
    ]
    response = await generate_openai_messages_response_with_tools(
        messages, tools=[], model=CODE_MODEL, max_tokens=2500, temperature=0.1
    )
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.IGNORECASE)
    plan = json.loads(cleaned)
    requested_paths = plan.get("files")
    if not isinstance(requested_paths, list):
        raise ValueError("Repository planner returned no file list")
    allowed = set(candidates)
    selected = []
    used = 0
    for raw_path in requested_paths[:MAX_FILES]:
        path = str(raw_path)
        if path not in allowed or any(existing == path for existing, _ in selected):
            continue
        content = _git_at(baseline, "show", f"{baseline}:{path}", max_chars=25_000)
        if selected and used + len(content) + len(path) > MAX_CONTEXT_CHARS:
            continue
        selected.append((path, content))
        used += len(content) + len(path)
    if not selected:
        raise ValueError("Repository planner selected no policy-allowed files")
    return selected, {
        "selection": "model_planner",
        "rationale": str(plan.get("rationale") or "")[:1000],
        "scope_check": str(plan.get("scope_check") or "")[:1000],
    }


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
    try:
        context, planning = await plan_code_context(request, baseline)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        context = select_code_context(request, baseline)
        planning = {"selection": "lexical_fallback"}
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
                f"BASELINE SHA: {baseline}\nREQUEST: {request}\n"
                f"REPOSITORY SCOPE PLAN: {planning}\n\n"
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
        **planning,
    }
