from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from providers.gemini_client import get_gemini_client, types
from services.memory_client import search_raw
import services.memory_utils as memory_utils

logger = logging.getLogger("gemini_utils")


class GeminiModerationError(Exception):
    def __init__(self, message, safety_ratings=None):
        super().__init__(message)
        self.safety_ratings = safety_ratings or []


REFUSAL_PATTERNS = [
    r"I cannot help you with that",
    r"I can't help you with that",
    r"I cannot provide that information",
    r"I can't provide that information",
    r"I am unable to provide",
    r"I cannot fulfill this request",
    r"I can't fulfill this request",
    r"I'm sorry, I can't help",
    r"I cannot discuss this topic",
]


_VISUAL_CODE_TERMS = (
    "plot",
    "graph",
    "chart",
    "draw",
    "diagram",
    "visualize",
    "visualise",
    "render",
    "image",
    "png",
    "svg",
    "figure",
    "circle",
    "circles",
    "concentric",
    "geometry",
    "matplotlib",
)

_ARTIFACT_BLOCK_RE = re.compile(
    r"BEGIN_ARTIFACT_BASE64\s*"
    r"filename:\s*(?P<filename>[^\n]+)\s*"
    r"mime:\s*(?P<mime>[^\n]+)\s*"
    r"data:\s*(?P<data>.*?)\s*"
    r"END_ARTIFACT_BASE64",
    re.DOTALL,
)


def _check_soft_refusal(text: str):
    if not text or len(text) > 300:
        return
    for pat in REFUSAL_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            logger.warning("Detected soft refusal in text: %s", text)
            raise GeminiModerationError(f"Model refused: {text}", safety_ratings=[])


def _looks_like_visual_code_request(prompt: str) -> bool:
    low = (prompt or "").lower()
    return any(term in low for term in _VISUAL_CODE_TERMS)


def _extract_base64_artifacts(text: str) -> Tuple[str, List[Tuple[bytes, str]]]:
    if not text:
        return text, []

    artifacts: List[Tuple[bytes, str]] = []

    def _replace(match: re.Match[str]) -> str:
        mime = (match.group("mime") or "application/octet-stream").strip()
        payload = re.sub(r"\s+", "", match.group("data") or "")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except Exception as e:
            logger.warning("Failed to decode Gemini base64 artifact block: %s", e)
            return ""
        artifacts.append((decoded, mime))
        return ""

    cleaned = _ARTIFACT_BLOCK_RE.sub(_replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, artifacts


def _summarize_execution_output(outputs: List[str], limit: int = 240) -> str:
    if not outputs:
        return ""
    text = (outputs[-1] or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def search_elasticsearch_resource(query_string: str, index: str = "discord_chat_memory", max_results: int = 10) -> str:
    try:
        resp = search_raw({"query_string": {"query": query_string}}, index=index, size=max_results)
        return json.dumps(resp, default=str)
    except Exception as e:
        return f"Error fetching ES resource: {e}"


def generate_gemini_text(
    prompt: str,
    context: Optional[List[Dict[str, str]]] = None,
    extra_parts: Optional[List[Any]] = None,
    status_tracker: Optional[Dict[str, str]] = None,
    enable_code_execution: bool = False,
    search_ids: Optional[Dict[str, Any]] = None,
    model_name: str = "gemini-2.0-flash",
) -> Tuple[Optional[str], List[Tuple[bytes, str]]]:
    client = get_gemini_client()
    if not client or not types:
        return None, []

    try:
        model = model_name
        artifact_required = enable_code_execution and _looks_like_visual_code_request(prompt)
        logger.info(
            "Generating text with model: %s (extra_parts=%s, code=%s)",
            model,
            len(extra_parts) if extra_parts else 0,
            enable_code_execution,
        )

        rag_context = ""
        if search_ids:
            clean_prompt = prompt.lower()
            trigger_words = ["first thing", "first message", "earliest", "beginning", "start", "history", "what did i say", "previous message", "recall", "remember"]
            if any(k in clean_prompt for k in trigger_words):
                try:
                    found_text = memory_utils.search_history_for_context(
                        guild_id=search_ids.get("guild_id"),
                        channel_id=search_ids.get("channel_id"),
                        user_id=search_ids.get("user_id"),
                        query_text=prompt,
                        limit=10,
                        oldest_first=any(k in clean_prompt for k in ["first", "earliest", "start", "beginning"]),
                    )
                    if found_text:
                        rag_context = (
                            "\n\n[SYSTEM: MEMORY RECALL]"
                            "\nThe user is asking about past events. Here is the relevant conversation history retrieved from the database:"
                            f"\n{found_text}\n"
                            "IMPORTANT: If this retrieved context is insufficient to answer specific requests (e.g., specific quotes, older messages, or details not shown above), "
                            "you MUST use the `search_elasticsearch_resource` tool to perform a specific search for the missing information.\n"
                            "[END MEMORY RECALL]\n"
                            "Use the above information to answer the user's question accurately.\n"
                        )
                    else:
                        rag_context = (
                            "\n\n[SYSTEM: MEMORY RECALL]"
                            "\nProactive database search returned NO direct matches for the user's specific query criteria (time range or keywords)."
                            "\nHowever, the user is explicitly asking for history."
                            "\nCRITICAL: Do NOT just say 'I don't recall'. You MUST use the `search_elasticsearch_resource` tool now with broader or different terms (e.g., ignore time, or search just keywords) to find the answer."
                            "\n[END MEMORY RECALL]\n"
                        )
                except Exception as e:
                    logger.warning("RAG search failed: %s", e)

        base_contents = []
        final_prompt_text = (rag_context + prompt) if rag_context else prompt
        if context:
            for msg in context:
                role = "user" if msg.get("role") == "user" else "model"
                base_contents.append(types.Content(role=role, parts=[types.Part(text=msg.get("content"))]))

        base_user_parts = [types.Part(text=final_prompt_text)]
        if extra_parts:
            base_user_parts.extend(extra_parts)
        if enable_code_execution:
            execution_instruction = (
                "\n(MANDATORY: You MUST use the code_execution tool to solve this request. "
                "Do not write code in markdown. Execute it."
            )
            if artifact_required:
                execution_instruction += (
                    " This request requires a visual result. Use matplotlib, render the requested figure, "
                    "and ensure the final result is returned as an inline image artifact. Call plt.show() "
                    "after creating the figure. Do not only print text, coordinates, or explanation."
                )
            execution_instruction += ")"
            base_user_parts.append(types.Part(text=execution_instruction))

        es_tool_spec = types.FunctionDeclaration(
            name="search_elasticsearch_resource",
            description="Query the internal Elasticsearch data store (Resources). Used to retrieve documents, archives, or logs not in current context.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query_string": types.Schema(type="STRING", description="Lucene query syntax, e.g. 'error AND service:auth'"),
                    "index": types.Schema(type="STRING", description="ES index name. Default is 'discord_chat_memory'."),
                    "max_results": types.Schema(type="INTEGER", description="Number of docs to return."),
                },
                required=["query_string"],
            ),
        )
        general_knowledge_tool = types.FunctionDeclaration(
            name="answer_general_knowledge",
            description="Use this tool to answer general knowledge questions, chit-chat, creative writing, or any request that does NOT require searching history or executing code. Provide your full, formatted answer in the 'answer' field.",
            parameters=types.Schema(
                type="OBJECT",
                properties={"answer": types.Schema(type="STRING", description="The full text of your answer to the user.")},
                required=["answer"],
            ),
        )

        tools_list = []
        search_keywords = ["search", "google", "web", "online", "news", "weather", "stock", "price", "current"]
        is_search_intent = any(k in prompt.lower() for k in search_keywords)
        should_add_functions = True
        if is_search_intent:
            try:
                tools_list.append(types.Tool(google_search=types.GoogleSearch()))
                should_add_functions = False
            except Exception as e:
                logger.warning("Failed to init google_search tool: %s", e)
        if enable_code_execution:
            try:
                if hasattr(types, "ToolCodeExecution"):
                    tools_list.append(types.Tool(code_execution=types.ToolCodeExecution()))
                else:
                    logger.warning("CodeExecution enabled but ToolCodeExecution type missing.")
                should_add_functions = False
            except Exception as e:
                logger.warning("Failed to init code_execution tool: %s", e)
        if should_add_functions:
            tools_list.append(types.Tool(function_declarations=[es_tool_spec, general_knowledge_tool]))

        sys_instructions = [
            "You are Multivac, a helpful AI assistant.",
            "You have access to tools. You MUST use a tool to respond.",
        ]
        if any(getattr(t, "function_declarations", None) for t in tools_list):
            sys_instructions.append(
                "If the user's request requires general knowledge, chit-chat, or creative writing (and does NOT need history or code execution), you MUST use the 'answer_general_knowledge' tool. "
                "You can search historical logs or memory using 'search_elasticsearch_resource'. "
                "IMPORTANT: If the user asks about 'history', 'past messages', 'first message', 'earliest interaction', or specific past details not in your current context, you MUST use 'search_elasticsearch_resource' to find the answer."
            )
        if any(getattr(t, "code_execution", None) for t in tools_list):
            sys_instructions.append(
                "You can perform live computations, file generation, or data processing using 'code_execution'. "
                "IMPORTANT: 1. You do NOT have a general knowledge tool. For general questions, lists, or text generation, use 'code_execution' to PRINT the answer. "
                "2. The sandbox does NOT have internet access or 'pydub'. "
                "3. For audio, use 'scipy.io.wavfile' and 'numpy'. "
                "4. For plotting, use 'matplotlib' or 'seaborn'."
            )
            if artifact_required:
                sys_instructions.append(
                    "For this request, a visual artifact is required. Generate and return an actual PNG file artifact. "
                    "Do not stop at prose, code blocks, or printed numeric output."
                )
        if any(getattr(t, "google_search", None) for t in tools_list):
            sys_instructions.append("You can search the live web using 'google_search'.")

        config = types.GenerateContentConfig(
            system_instruction=" ".join(sys_instructions),
            tools=tools_list,
            safety_settings=[
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            ],
        )

        def _build_attempt_contents(extra_instruction: Optional[str] = None):
            attempt_parts = list(base_user_parts)
            if extra_instruction:
                attempt_parts.append(types.Part(text=extra_instruction))
            attempt_contents = list(base_contents)
            attempt_contents.append(types.Content(role="user", parts=attempt_parts))
            return attempt_contents

        def _run_attempt(extra_instruction: Optional[str] = None):
            final_text = []
            generated_artifacts = []
            code_result_outputs = []
            saw_executable_code = False
            current_lang = "python"
            part_events: List[str] = []
            response_stream = client.models.generate_content_stream(
                model=model,
                contents=_build_attempt_contents(extra_instruction),
                config=config,
            )

            for chunk in response_stream:
                if chunk.candidates:
                    cand = chunk.candidates[0]
                    if cand.finish_reason in ["SAFETY", "kFinishReasonSafety"]:
                        raise GeminiModerationError(
                            f"Response blocked by safety filters (reason={cand.finish_reason}).",
                            getattr(cand, "safety_ratings", []),
                        )

                if not chunk.candidates:
                    continue
                for part in chunk.candidates[0].content.parts:
                    if part.text:
                        part_events.append(f"text:{len(part.text)}")
                        final_text.append(part.text)
                    if part.inline_data:
                        part_events.append(f"inline_data:{part.inline_data.mime_type}:{len(part.inline_data.data or b'')}")
                        generated_artifacts.append((part.inline_data.data, part.inline_data.mime_type))
                    if part.executable_code:
                        saw_executable_code = True
                        code_chunk = part.executable_code.code
                        if part.executable_code.language:
                            current_lang = part.executable_code.language.lower()
                        part_events.append(f"executable_code:{current_lang}:{len(code_chunk or '')}")
                        if status_tracker is not None:
                            snippet = "\n".join((code_chunk or "").splitlines()[-6:]) or "..."
                            status_tracker["text"] = f"Writing Code...\n```{current_lang}\n{snippet}\n```"
                    if part.function_call:
                        if part.function_call.name == "answer_general_knowledge":
                            part_events.append("function_call:answer_general_knowledge")
                            args = part.function_call.args
                            if args and "answer" in args:
                                final_text.append(args["answer"])
                        else:
                            part_events.append(f"function_call:{part.function_call.name}")
                            try:
                                arg_str = str(part.function_call.args)
                            except Exception:
                                arg_str = "{...}"
                            final_text.append(f"🛠️ `[Tool Call: {part.function_call.name}({arg_str})]`")
                    if part.code_execution_result:
                        outcome = part.code_execution_result.outcome
                        output = part.code_execution_result.output.strip()
                        part_events.append(f"code_execution_result:{outcome}:{len(output)}")
                        if output:
                            code_result_outputs.append(output)
                        if status_tracker is not None:
                            status_tracker["text"] = f"Executed: {outcome}\nResult: {output[:50]}..."

            cleaned_text = "".join(final_text).strip() if final_text else None
            recovered_artifacts: List[Tuple[bytes, str]] = []
            if cleaned_text:
                cleaned_text, extracted = _extract_base64_artifacts(cleaned_text)
                recovered_artifacts.extend(extracted)

            cleaned_outputs = []
            for output in code_result_outputs:
                cleaned_output, extracted = _extract_base64_artifacts(output)
                recovered_artifacts.extend(extracted)
                if cleaned_output:
                    cleaned_outputs.append(cleaned_output)

            if recovered_artifacts:
                part_events.append(f"recovered_artifacts:{len(recovered_artifacts)}")
                generated_artifacts.extend(recovered_artifacts)

            logger.info(
                "Gemini code attempt summary: artifact_required=%s saw_code=%s artifacts=%d parts=%s",
                artifact_required,
                saw_executable_code,
                len(generated_artifacts),
                part_events,
            )

            return {
                "final_text": cleaned_text,
                "generated_artifacts": generated_artifacts,
                "code_result_outputs": cleaned_outputs,
                "saw_executable_code": saw_executable_code,
                "part_events": part_events,
            }

        attempt = _run_attempt()
        if artifact_required and attempt["saw_executable_code"] and not attempt["generated_artifacts"]:
            logger.warning("Visual code request executed without artifact; retrying with stricter render instructions.")
            attempt = _run_attempt(
                "RETRY: The previous execution did not return an inline image. Re-run using matplotlib only, "
                "use a minimal script with matplotlib.pyplot and basic matplotlib.patches primitives only, "
                "avoid external files, avoid PIL, avoid seaborn, avoid numpy unless absolutely necessary, "
                "draw the requested figure, call plt.axis('equal') if geometry matters, call plt.show(), "
                "and make the image itself the primary output. Also emit a fallback PNG payload in exactly this format: "
                "BEGIN_ARTIFACT_BASE64 newline "
                "filename: render.png newline "
                "mime: image/png newline "
                "data: <single-line base64 PNG> newline "
                "END_ARTIFACT_BASE64. Do not wrap the payload in markdown fences."
            )

        final_text = attempt["final_text"]
        generated_artifacts = attempt["generated_artifacts"]
        code_result_outputs = attempt["code_result_outputs"]
        saw_executable_code = attempt["saw_executable_code"]
        execution_summary = _summarize_execution_output(code_result_outputs)

        if artifact_required and saw_executable_code and not generated_artifacts:
            logger.warning(
                "Gemini visual code request still returned no artifact after retry. Parts=%s execution_output=%r",
                attempt["part_events"],
                execution_summary,
            )
            if execution_summary:
                return f"Gemini code execution failed before producing an image: {execution_summary}", []
            return "Code executed, but no image artifact was returned.", []

        if generated_artifacts and enable_code_execution:
            summary_text = "Generated artifact attached."
            if artifact_required:
                summary_text = "Rendered image attached."
            return summary_text, generated_artifacts

        if final_text or generated_artifacts:
            full_text = "".join(final_text).strip() if final_text else None
            if not full_text and not generated_artifacts and code_result_outputs:
                full_text = code_result_outputs[-1]
            _check_soft_refusal(full_text)
            return full_text, generated_artifacts

        if code_result_outputs:
            if artifact_required and saw_executable_code:
                if execution_summary:
                    return f"Gemini code execution failed before producing an image: {execution_summary}", []
                return "Code executed, but no image artifact was returned.", []
            full_text = code_result_outputs[-1]
            _check_soft_refusal(full_text)
            return full_text, []

        logger.warning("Gemini text generation returned no text in stream.")
    except Exception as e:
        if isinstance(e, GeminiModerationError):
            raise
        logger.error("Gemini text generation failed: %s", e)
        return None, []

    return None, []
