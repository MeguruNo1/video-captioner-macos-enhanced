"""Per-video hotwords and a safely managed persistent translation glossary."""
import re
from pathlib import Path
from uuid import uuid4

from app.core.utils.transcript_terms import extract_glossary_pairs

AUTO_BEGIN = "<!-- VIDEOCAPTIONER_MCP_TERMS_BEGIN -->"
AUTO_END = "<!-- VIDEOCAPTIONER_MCP_TERMS_END -->"
from app.core.utils.platform_utils import IS_MACOS, app_data_dir

DEFAULT_GLOSSARY_PATH = (Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Note/Translate/对照.md"
                         if IS_MACOS else app_data_dir("VideoCaptioner") / "glossary.md")
COMMON_SENTENCE_WORDS = {"a", "an", "and", "but", "he", "her", "here", "i", "if", "it", "my", "no", "now", "oh", "okay", "she", "so", "that", "the", "then", "they", "this", "we", "well", "what", "when", "why", "you", "your"}


def read_glossary(path):
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return "", []
    return text, extract_glossary_pairs(text)


def _appears(term, transcript):
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", transcript, re.I))


def extract_task_terms(transcript, glossary_path, limit=80):
    """Match maintained names first, then add conservative capitalized candidates."""
    _, pairs = read_glossary(glossary_path)
    matched = [(source, target) for source, target in pairs if _appears(source, transcript)]
    values = [source for source, _ in matched]
    seen = {value.casefold() for value in values}
    pattern = re.compile(r"\b(?:[A-Z][A-Za-z0-9+#'-]*)(?:\s+(?:[A-Z][A-Za-z0-9+#'-]*|of|the|and)){0,4}\b")
    for match in pattern.finditer(transcript):
        candidate = match.group(0).strip(" '-")
        key = candidate.casefold()
        if len(candidate) < 3 or key in COMMON_SENTENCE_WORDS or key in seen:
            continue
        seen.add(key)
        values.append(candidate)
        if len(values) >= limit:
            break
    return {"hotwords": ", ".join(values[:limit]), "glossary": dict(matched),
            "candidates": values[:limit], "glossary_path": str(Path(glossary_path).expanduser())}


def update_glossary_file(path, additions):
    """Append confirmed pairs inside one managed block, preserving manual notes."""
    if not path or not additions:
        return []
    path = Path(path).expanduser()
    if not path.is_file():
        return []
    text, pairs = read_glossary(path)
    existing = {source.casefold() for source, _ in pairs}
    accepted = []
    for source, target in additions.items():
        source, target = str(source).strip(), str(target).strip()
        if source and target and source.casefold() not in existing:
            existing.add(source.casefold())
            accepted.append((source, target))
    if not accepted:
        return []
    block_pattern = re.compile(rf"\n*{re.escape(AUTO_BEGIN)}.*?{re.escape(AUTO_END)}\n*", re.S)
    old_block = block_pattern.search(text)
    managed_pairs = extract_glossary_pairs(old_block.group(0)) if old_block else []
    block = "\n\n## MCP 自动收录\n" + AUTO_BEGIN + "\n" + "\n".join(
        f"{source} → {target}  " for source, target in managed_pairs + accepted
    ) + "\n" + AUTO_END + "\n"
    updated = block_pattern.sub("\n", text).rstrip() + block
    temporary = path.with_name(f".{path.name}.videocaptioner-{uuid4().hex}.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return accepted
