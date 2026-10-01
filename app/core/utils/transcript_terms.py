import hashlib
import json
import re
import unicodedata
from pathlib import Path
from string import Template
from typing import Any, Literal

from app.core.utils import json_repair
from app.core.utils.openai_compat import get_openai_compat_request_options


GENERATED_TERMS_BEGIN = "<!-- AI_VIDEO_TRANSCRIPT_TERMS_BEGIN -->"
GENERATED_TERMS_END = "<!-- AI_VIDEO_TRANSCRIPT_TERMS_END -->"
MAX_FILTERED_PROMPT_TERMS = 80
MAX_FILTERED_PROMPT_CHARS = 4000
GLOSSARY_SEPARATORS = ("->", "=>", "→", "=", "：", ":")


def _clean_term_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n`'\"“”‘’")


def extract_glossary_pairs(glossary_text: str) -> list[tuple[str, str]]:
    """Extract glossary pairs from plain text or a Markdown note."""
    pairs = []
    seen = set()
    for raw_line in str(glossary_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "```")):
            continue

        line = re.sub(r"^[-*+]\s+", "", line)
        for separator in GLOSSARY_SEPARATORS:
            if separator in line:
                original, translation = line.split(separator, 1)
                original = _clean_term_text(original)
                translation = _clean_term_text(translation)
                if not original or not translation:
                    break

                # Markdown navigation and URLs often contain a colon but are
                # not glossary entries.
                if separator in ("：", ":") and (
                    "http://" in line
                    or "https://" in line
                    or "[[" in line
                    or "](" in line
                ):
                    break

                key = original.casefold()
                if key not in seen:
                    seen.add(key)
                    pairs.append((original, translation))
                break
    return pairs


def format_glossary_pairs(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{original} -> {translation}" for original, translation in pairs)


def parse_glossary_text(glossary_text: str) -> dict[str, str]:
    glossary = {}
    for original, translation in extract_glossary_pairs(glossary_text):
        glossary[original.casefold()] = translation
    return glossary


def _normalize_term_for_match(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean_term_text(value)).casefold()
    return "".join(ch for ch in text if ch.isalnum() or ch in "+#")


def _find_glossary_translation(glossary: dict[str, str], original: str) -> str:
    exact_key = _clean_term_text(original).casefold()
    if exact_key in glossary:
        return glossary[exact_key]

    normalized_key = _normalize_term_for_match(original)
    if not normalized_key:
        return ""

    matches = [
        translation
        for glossary_original, translation in glossary.items()
        if _normalize_term_for_match(glossary_original) == normalized_key
    ]
    unique_matches = set(matches)
    if len(unique_matches) == 1:
        return matches[0]
    return ""


def parse_hotwords_text(hotwords_text: str) -> list[str]:
    values = []
    seen = set()
    for value in re.split(r"[,，;\n\r]+", str(hotwords_text or "")):
        text = _clean_term_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            values.append(text)
    return values


def parse_ai_terms_response(response_text: str, glossary_text: str = "") -> list[dict[str, str]]:
    data = json_repair.loads(response_text)
    raw_terms = data.get("terms", data) if isinstance(data, dict) else data
    glossary = parse_glossary_text(glossary_text)
    terms = []
    seen = set()

    for item in raw_terms or []:
        if isinstance(item, str):
            original = _clean_term_text(item)
            translation = ""
            category = "term"
        elif isinstance(item, dict):
            original = _clean_term_text(
                item.get("original")
                or item.get("source")
                or item.get("name")
                or item.get("term")
            )
            translation = _clean_term_text(
                item.get("translation") or item.get("target") or item.get("zh")
            )
            category = _clean_term_text(item.get("category") or item.get("type") or "term")
        else:
            continue

        if not original:
            continue
        translation = _find_glossary_translation(glossary, original) or translation
        key = original.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(
            {
                "original": original,
                "translation": translation,
                "category": category or "term",
            }
        )

    return terms


def merge_hotwords(existing_hotwords: str, terms: list[dict[str, str]]) -> str:
    values = parse_hotwords_text(existing_hotwords)
    seen = {value.casefold() for value in values}
    for term in terms:
        text = _clean_term_text(term.get("original"))
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            values.append(text)
    return ", ".join(values)


def format_hotwords_from_terms(terms: list[dict[str, str]]) -> str:
    values = []
    seen = set()
    for term in terms:
        text = _clean_term_text(term.get("original"))
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            values.append(text)
    return ", ".join(values)


def format_terms_for_document_prompt(terms: list[dict[str, str]]) -> str:
    rows = []
    for term in terms:
        original = _clean_term_text(term.get("original"))
        translation = _clean_term_text(term.get("translation"))
        if not original:
            continue
        if translation:
            rows.append(f"- {original} -> {translation}")
        else:
            rows.append(f"- {original}")
    if not rows:
        return ""
    return "\n".join(
        [
            GENERATED_TERMS_BEGIN,
            "WhisperX 热词生成的翻译术语，翻译与校正时优先遵循：",
            *rows,
            GENERATED_TERMS_END,
        ]
    )


def merge_document_prompt(existing_prompt: str, terms: list[dict[str, str]]) -> str:
    generated = format_terms_for_document_prompt(terms)
    existing = str(existing_prompt or "").strip()
    pattern = re.compile(
        rf"\n*{re.escape(GENERATED_TERMS_BEGIN)}.*?{re.escape(GENERATED_TERMS_END)}\n*",
        re.S,
    )
    existing = pattern.sub("\n", existing).strip()
    if not generated:
        return existing
    if existing:
        return f"{existing}\n\n{generated}"
    return generated


def remove_generated_document_prompt_terms(existing_prompt: str) -> str:
    pattern = re.compile(
        rf"\n*{re.escape(GENERATED_TERMS_BEGIN)}.*?{re.escape(GENERATED_TERMS_END)}\n*",
        re.S,
    )
    return pattern.sub("\n", str(existing_prompt or "")).strip()


def _split_generated_terms_block(prompt: str) -> tuple[str, str]:
    text = str(prompt or "")
    pattern = re.compile(
        rf"{re.escape(GENERATED_TERMS_BEGIN)}(.*?){re.escape(GENERATED_TERMS_END)}",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        return text.strip(), ""

    user_prompt = (text[: match.start()] + text[match.end() :]).strip()
    return user_prompt, match.group(1)


def _parse_document_prompt_terms(block: str) -> list[dict[str, str]]:
    terms = []
    for raw_line in str(block or "").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("-"):
            continue

        line = line.lstrip("-").strip()
        original = ""
        translation = ""
        for separator in ("->", "=>", "=", "：", ":"):
            if separator in line:
                original, translation = line.split(separator, 1)
                break
        else:
            original = line

        original = _clean_term_text(original)
        translation = _clean_term_text(translation)
        if original:
            terms.append({"original": original, "translation": translation})
    return terms


def _term_matches_text(term: str, text: str) -> bool:
    term = _clean_term_text(term)
    if not term:
        return False

    if re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", term):
        return term in text

    if re.search(r"[A-Za-z0-9]", term):
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        return bool(pattern.search(text))

    return term in text


def filter_document_prompt_for_text(
    prompt: str, text: str, mode: Literal["translation", "correction"] = "translation"
) -> str:
    """
    Keep user-written prompt text, but reduce the generated AI term block to
    terms relevant to the current subtitle batch.

    In translation mode, keep only terms whose source name appears in the
    current batch. In correction mode, keep source-name candidates even when
    they do not appear exactly, because the ASR text may be misspelled.
    """
    if mode not in {"translation", "correction"}:
        raise ValueError(f"Unsupported document prompt filter mode: {mode}")

    user_prompt, generated_block = _split_generated_terms_block(prompt)
    terms = _parse_document_prompt_terms(generated_block)
    if not terms:
        return str(prompt or "").strip()

    source_text = str(text or "")
    matched_terms = []
    seen = set()
    for term in terms:
        original = _clean_term_text(term.get("original"))
        key = original.casefold()
        if not original or key in seen:
            continue
        if mode == "correction" or _term_matches_text(original, source_text):
            seen.add(key)
            matched_terms.append(term)
        if len(matched_terms) >= MAX_FILTERED_PROMPT_TERMS:
            break

    generated = format_terms_for_document_prompt(matched_terms)
    parts = [part for part in (user_prompt, generated) if part]
    filtered_prompt = "\n\n".join(parts).strip()
    if len(filtered_prompt) > MAX_FILTERED_PROMPT_CHARS:
        filtered_prompt = filtered_prompt[:MAX_FILTERED_PROMPT_CHARS].rstrip()
    return filtered_prompt


def format_terms_txt(terms: list[dict[str, str]]) -> str:
    lines = ["原文\t译文\t类型"]
    for term in terms:
        lines.append(
            "\t".join(
                [
                    _clean_term_text(term.get("original")),
                    _clean_term_text(term.get("translation")),
                    _clean_term_text(term.get("category")),
                ]
            )
        )
    return "\n".join(lines)


def _resolve_current_llm_settings() -> dict[str, Any]:
    from app.common.config import cfg
    from app.core.entities import LLMServiceEnum

    current_service = cfg.llm_service.value
    if current_service == LLMServiceEnum.OPENAI:
        base_url = cfg.openai_api_base.value
        api_key = cfg.openai_api_key.value
        model = cfg.openai_model.value
    elif current_service == LLMServiceEnum.SILICON_CLOUD:
        base_url = cfg.silicon_cloud_api_base.value
        api_key = cfg.silicon_cloud_api_key.value
        model = cfg.silicon_cloud_model.value
    elif current_service == LLMServiceEnum.DEEPSEEK:
        base_url = cfg.deepseek_api_base.value
        api_key = cfg.deepseek_api_key.value
        model = cfg.deepseek_model.value
    elif current_service == LLMServiceEnum.OLLAMA:
        base_url = cfg.ollama_api_base.value
        api_key = cfg.ollama_api_key.value
        model = cfg.ollama_model.value
    elif current_service == LLMServiceEnum.LM_STUDIO:
        base_url = cfg.lm_studio_api_base.value
        api_key = cfg.lm_studio_api_key.value
        model = cfg.lm_studio_model.value
    elif current_service == LLMServiceEnum.GEMINI:
        base_url = cfg.gemini_api_base.value
        api_key = cfg.gemini_api_key.value
        model = cfg.gemini_model.value
    elif current_service == LLMServiceEnum.CHATGLM:
        base_url = cfg.chatglm_api_base.value
        api_key = cfg.chatglm_api_key.value
        model = cfg.chatglm_model.value
    elif current_service == LLMServiceEnum.QWEN:
        base_url = cfg.qwen_api_base.value
        api_key = cfg.qwen_api_key.value
        model = cfg.qwen_model.value
    else:
        base_url = ""
        api_key = ""
        model = ""

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "service": current_service.value,
        "timeout": cfg.llm_request_timeout.value,
        "qwen_enable_thinking": cfg.qwen_enable_thinking.value,
    }


def _build_term_extraction_messages(
    transcript_text: str, glossary_text: str, target_language: str
) -> list[dict[str, str]]:
    system_prompt = """
你是视频字幕术语整理助手。请从视频文稿中提取人名、角色名、组织名、产品名、作品名、地点名、专业术语和高频专有名词。

规则：
- 优先使用用户词库中的译名；词库没有时，根据上下文给出适合${target_language}字幕翻译的译名。
- 如果 AI 判断与用户词库存在冲突、争议或不确定，必须以用户词库为准，不要自行改写词库译名。
- 只保留对转录纠错或字幕翻译有帮助的名称/术语。
- 不要提取普通虚词、泛泛名词或完整句子。
- 原文必须保留文稿中的写法或最可能的标准写法。
- 最多返回 80 条。
- 只返回纯 JSON，不要 Markdown，不要解释文字。

输出格式：
{
  "terms": [
    {"original": "Original Name", "translation": "译名", "category": "person|organization|place|product|work|term"}
  ]
}
"""
    user_content = "\n".join(
        [
            "用户词库：",
            glossary_text.strip() or "（空）",
            "",
            "视频文稿：",
            transcript_text,
        ]
    )
    return [
        {
            "role": "system",
            "content": Template(system_prompt).safe_substitute(
                target_language=target_language or "目标语言"
            ),
        },
        {"role": "user", "content": user_content},
    ]


def extract_terms_with_ai(
    transcript_text: str,
    glossary_text: str,
    target_language: str,
) -> list[dict[str, str]]:
    settings = _resolve_current_llm_settings()
    if not settings["base_url"] or not settings["api_key"] or not settings["model"]:
        raise ValueError("LLM API 未配置，无法提取视频文稿术语")

    from openai import OpenAI

    messages = _build_term_extraction_messages(
        transcript_text[:24000],
        glossary_text,
        target_language,
    )
    client = OpenAI(base_url=settings["base_url"], api_key=settings["api_key"])
    response = client.chat.completions.create(
        model=settings["model"],
        messages=messages,
        temperature=0.1,
        **get_openai_compat_request_options(
            service_name=settings["service"],
            model_name=settings["model"],
            qwen_enable_thinking=settings["qwen_enable_thinking"],
            default_timeout=settings["timeout"],
        ),
    )
    return parse_ai_terms_response(response.choices[0].message.content, glossary_text)


def _build_hotword_translation_messages(
    hotwords: list[str], target_language: str, glossary_text: str = ""
) -> list[dict[str, str]]:
    system_prompt = """
你是字幕翻译术语整理助手。请把用户人工校正后的 WhisperX 热词整理成翻译阶段使用的术语对照表。

规则：
- 每个输入热词都视为原文术语候选。
- 为每个热词生成适合${target_language}字幕翻译的译名，输出必须是“原文 -> 译文”的术语对照含义。
- 用户词库中已有匹配项时，必须使用用户词库译名，不要自行改写。
- 如果目标语言是中文，作品名、角色名、组织名、地点名、商品名和常见专有名词应优先给出通用中文译名、官方译名或音译名。
- 只有命令、代码、变量、文件名、版本号、模型编号、品牌标识，或确实没有自然译名的专名，译名才可以与原文相同。
- 不要因为输入是英文专名就默认照抄为译名。
- 不要新增输入列表之外的术语。
- 只返回纯 JSON，不要 Markdown，不要解释文字。

输出格式：
{
  "terms": [
    {"original": "Original Name", "translation": "译名", "category": "term"}
  ]
}
"""
    user_content = "\n".join(
        [
            "用户词库：",
            glossary_text.strip() or "（空）",
            "",
            "WhisperX 热词：",
            "\n".join(f"- {hotword}" for hotword in hotwords),
        ]
    )
    return [
        {
            "role": "system",
            "content": Template(system_prompt).safe_substitute(
                target_language=target_language or "目标语言"
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _split_hotwords_by_glossary(
    hotwords: list[str], glossary_text: str
) -> tuple[list[dict[str, str]], list[str]]:
    glossary = parse_glossary_text(glossary_text)
    matched_terms = []
    unmatched_hotwords = []

    for hotword in hotwords:
        original = _clean_term_text(hotword)
        if not original:
            continue

        translation = _find_glossary_translation(glossary, original)
        if translation:
            matched_terms.append(
                {
                    "original": original,
                    "translation": translation,
                    "category": "term",
                }
            )
        else:
            unmatched_hotwords.append(original)

    return matched_terms, unmatched_hotwords


def extract_translation_terms_from_hotwords(
    hotwords_text: str,
    target_language: str,
    glossary_text: str = "",
) -> list[dict[str, str]]:
    hotwords = parse_hotwords_text(hotwords_text)
    if not hotwords:
        return []
    limited_hotwords = hotwords[:MAX_FILTERED_PROMPT_TERMS]

    glossary_terms, unmatched_hotwords = _split_hotwords_by_glossary(
        limited_hotwords,
        glossary_text,
    )
    if not unmatched_hotwords:
        return glossary_terms

    settings = _resolve_current_llm_settings()
    if not settings["base_url"] or not settings["api_key"] or not settings["model"]:
        raise ValueError("LLM API 未配置，无法从 WhisperX 热词生成翻译术语")

    from openai import OpenAI

    messages = _build_hotword_translation_messages(
        unmatched_hotwords,
        target_language,
        glossary_text,
    )
    client = OpenAI(base_url=settings["base_url"], api_key=settings["api_key"])
    response = client.chat.completions.create(
        model=settings["model"],
        messages=messages,
        temperature=0.1,
        **get_openai_compat_request_options(
            service_name=settings["service"],
            model_name=settings["model"],
            qwen_enable_thinking=settings["qwen_enable_thinking"],
            default_timeout=settings["timeout"],
        ),
    )
    ai_terms = parse_ai_terms_response(response.choices[0].message.content)
    unmatched_keys = {hotword.casefold() for hotword in unmatched_hotwords}
    terms_by_key = {
        _clean_term_text(term.get("original")).casefold(): term
        for term in ai_terms
        if _clean_term_text(term.get("original")).casefold() in unmatched_keys
    }

    ordered_ai_terms = [
        terms_by_key[key]
        for key in (hotword.casefold() for hotword in unmatched_hotwords)
        if key in terms_by_key
    ]
    merged_terms_by_key = {
        _clean_term_text(term.get("original")).casefold(): term
        for term in glossary_terms + ordered_ai_terms
    }
    return [
        merged_terms_by_key[key]
        for key in (hotword.casefold() for hotword in limited_hotwords)
        if key in merged_terms_by_key
    ]


def apply_terms_to_whisperx_hotwords(terms: list[dict[str, str]]) -> dict[str, str]:
    from app.common.config import cfg

    hotwords = format_hotwords_from_terms(terms)
    document_prompt = remove_generated_document_prompt_terms(cfg.custom_prompt_text.value)
    cfg.set(cfg.whisperx_hotwords, hotwords)
    cfg.set(cfg.custom_prompt_text, document_prompt)
    return {"whisperx_hotwords": hotwords, "custom_prompt_text": document_prompt}


def apply_terms_to_mlx_hotwords(terms: list[dict[str, str]]) -> dict[str, str]:
    from app.common.config import cfg

    hotwords = format_hotwords_from_terms(terms)
    document_prompt = remove_generated_document_prompt_terms(cfg.custom_prompt_text.value)
    cfg.set(cfg.mlx_hotwords, hotwords)
    cfg.set(cfg.custom_prompt_text, document_prompt)
    return {"mlx_hotwords": hotwords, "custom_prompt_text": document_prompt}


def apply_terms_to_document_prompt(terms: list[dict[str, str]]) -> dict[str, str]:
    from app.common.config import cfg

    document_prompt = merge_document_prompt(cfg.custom_prompt_text.value, terms)
    cfg.set(cfg.custom_prompt_text, document_prompt)
    return {"custom_prompt_text": document_prompt}


def apply_terms_to_prompt_settings(terms: list[dict[str, str]]) -> dict[str, str]:
    result = apply_terms_to_whisperx_hotwords(terms)
    result.update(apply_terms_to_document_prompt(terms))
    return result


def write_terms_txt_file(terms: list[dict[str, str]], work_dir: Path, filename_stem: str) -> str:
    digest = hashlib.md5(format_terms_txt(terms).encode("utf-8")).hexdigest()[:8]
    terms_path = work_dir / f"【AI术语表】{filename_stem}-{digest}.txt"
    work_dir.mkdir(parents=True, exist_ok=True)
    terms_path.write_text(format_terms_txt(terms), encoding="utf-8")
    return str(terms_path)
