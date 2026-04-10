#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — prompt language processor.

Behavior:
  - Chinese input        → translate to English, save to YYYY_MM_DD_whole_translate.md
  - English input        → grammar / word-choice / spelling check, save to YYYY_MM_DD_partial_correction.md
  - English + Chinese    → translate inline Chinese parts, include in grammar check,
                           save to YYYY_MM_DD_partial_correction.md

Log files are appended (created on first use) under ~/claude_prompt_logs/.
"""

import sys
import json
import os
import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / "claude_prompt_logs"


def ensure_log_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def append_to_file(filepath: Path, content: str) -> None:
    with open(filepath, "a", encoding="utf-8") as fh:
        fh.write(content)


def detect_language(text: str) -> str:
    """Return 'chinese', 'english', or 'mixed_english_start'."""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    english_chars = len(re.findall(r"[a-zA-Z]", text))

    if chinese_chars > 0 and english_chars == 0:
        return "chinese"
    if chinese_chars > 0 and english_chars > 0:
        first_letter = re.search(r"\S", text)
        if first_letter and re.match(r"[a-zA-Z]", first_letter.group()):
            return "mixed_english_start"
        return "chinese"  # Chinese-led mixed → treat as Chinese
    return "english"


def call_claude(system: str, user: str) -> str:
    """Call the Claude API and return the assistant's text."""
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def safe_parse_json(raw: str) -> dict:
    """Strip optional markdown fences then parse JSON."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Chinese path
# ---------------------------------------------------------------------------

TRANSLATE_SYSTEM = (
    "You are a Chinese-to-English translator. "
    "Translate the following Chinese text into natural, fluent English. "
    "Reply with ONLY the English translation — no explanations, no labels."
)


def process_chinese(prompt: str, ts: str, date: str) -> tuple[str, str]:
    """Translate Chinese → English. Returns (translation, filepath)."""
    translation = call_claude(TRANSLATE_SYSTEM, prompt)

    log_dir = ensure_log_dir()
    filepath = log_dir / f"{date}_whole_translate.md"
    sep = "\n---\n" if filepath.exists() else ""

    content = (
        f"{sep}## Translation Entry — {ts}\n\n"
        f"**Original Chinese:**\n{prompt}\n\n"
        f"**English Translation:**\n{translation}\n"
    )
    append_to_file(filepath, content)
    return translation, str(filepath)


# ---------------------------------------------------------------------------
# English / mixed path
# ---------------------------------------------------------------------------

GRAMMAR_SYSTEM_ENGLISH = """\
You are an English language expert. Analyze the given text for grammar issues,
word-choice issues, and spelling errors.

Reply ONLY with a JSON object in this exact schema (no markdown fences):
{
  "has_issues": <bool>,
  "issues": [
    {
      "type": "grammar",
      "problem": "<description of the grammar problem>",
      "original": "<the problematic fragment>",
      "corrected": "<corrected version>"
    },
    {
      "type": "word_choice",
      "original_word": "<original word>",
      "corrected_word": "<better word>",
      "chinese_meaning": "<中文意思>",
      "reason": "<brief reason>"
    },
    {
      "type": "spelling",
      "original_word": "<misspelled word>",
      "corrected_word": "<correct word>",
      "chinese_meaning": "<中文意思>"
    }
  ]
}
If no issues exist, set has_issues to false and issues to [].
"""

GRAMMAR_SYSTEM_MIXED = """\
You are an English language expert and translator.
The input text is written mainly in English but contains some Chinese segments.

Your tasks:
1. Find every Chinese segment and produce an inline translation.
2. Check the full text (treating Chinese parts as translated) for grammar issues,
   word-choice issues, and spelling errors.

Reply ONLY with a JSON object in this exact schema (no markdown fences):
{
  "has_issues": <bool>,
  "chinese_translations": [
    {"chinese": "<original Chinese segment>", "english": "<English translation>"}
  ],
  "issues": [
    {
      "type": "grammar",
      "problem": "<description>",
      "original": "<fragment>",
      "corrected": "<correction>"
    },
    {
      "type": "word_choice",
      "original_word": "<word>",
      "corrected_word": "<better word>",
      "chinese_meaning": "<中文意思>",
      "reason": "<brief reason>"
    },
    {
      "type": "spelling",
      "original_word": "<misspelled>",
      "corrected_word": "<correct>",
      "chinese_meaning": "<中文意思>"
    }
  ]
}
If no issues exist, set has_issues to false and issues to [].
"""


def build_issue_lines(result: dict) -> list[str]:
    """Turn the parsed JSON result into human-readable lines."""
    lines: list[str] = []

    if not result.get("has_issues") or not result.get("issues"):
        lines.append("Completely correct usage. (完全正确的用法)")
        return lines

    issues = result["issues"]
    grammar = [i for i in issues if i["type"] == "grammar"]
    word_choice = [i for i in issues if i["type"] == "word_choice"]
    spelling = [i for i in issues if i["type"] == "spelling"]

    n = 1
    if grammar:
        lines.append("**Grammar Issues:**")
        for i in grammar:
            lines.append(f"{n}. Grammar problem: {i['problem']}")
            lines.append(f"   - Original:  \"{i['original']}\"")
            lines.append(f"   - Corrected: \"{i['corrected']}\"")
            n += 1

    if word_choice:
        lines.append("\n**Word Choice Issues:**")
        for i in word_choice:
            lines.append(
                f"{n}. Word choice: \"{i['original_word']}\" → \"{i['corrected_word']}\" "
                f"({i['chinese_meaning']})"
            )
            lines.append(f"   - Reason: {i['reason']}")
            n += 1

    if spelling:
        lines.append("\n**Spelling Errors:**")
        for i in spelling:
            lines.append(
                f"{n}. Spelling: \"{i['original_word']}\" → \"{i['corrected_word']}\" "
                f"({i['chinese_meaning']})"
            )
            n += 1

    return lines


def process_english(prompt: str, ts: str, date: str, lang: str) -> tuple[str, str]:
    """Grammar-check (and optionally translate Chinese parts). Returns (display_text, filepath)."""
    system = GRAMMAR_SYSTEM_MIXED if lang == "mixed_english_start" else GRAMMAR_SYSTEM_ENGLISH
    raw = call_claude(system, prompt)
    result = safe_parse_json(raw)

    display_lines: list[str] = []
    file_lines: list[str] = []

    # Chinese inline translations (mixed mode only)
    translations = result.get("chinese_translations", [])
    if translations:
        display_lines.append("**Chinese Parts Translated:**")
        file_lines.append("**Chinese Parts Translated:**")
        for t in translations:
            entry = f"- {t['chinese']} → {t['english']}"
            display_lines.append(entry)
            file_lines.append(entry)
        display_lines.append("")
        file_lines.append("")

    # Grammar / spelling check result
    issue_lines = build_issue_lines(result)
    display_lines.append("**Grammar / Spelling Check:**")
    file_lines.append("**Grammar / Spelling Check:**")
    display_lines.extend(issue_lines)
    file_lines.extend(issue_lines)

    display_text = "\n".join(display_lines)

    # Write to log
    log_dir = ensure_log_dir()
    filepath = log_dir / f"{date}_partial_correction.md"
    sep = "\n---\n" if filepath.exists() else ""

    log_content = (
        f"{sep}## Correction Entry — {ts}\n\n"
        f"**Original Prompt:**\n{prompt}\n\n"
        + "\n".join(file_lines)
        + "\n"
    )
    append_to_file(filepath, log_content)
    return display_text, str(filepath)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt: str = data.get("prompt", "").strip()
    if not prompt:
        sys.exit(0)

    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    date = now.strftime("%Y_%m_%d")

    try:
        lang = detect_language(prompt)

        if lang == "chinese":
            translation, filepath = process_chinese(prompt, ts, date)
            context = (
                "[Prompt Pre-processing]\n"
                f"Language detected: Chinese\n\n"
                f"**English Translation:**\n{translation}\n\n"
                f"(Saved to: {filepath})\n"
            )
        elif lang == "mixed_english_start":
            display, filepath = process_english(prompt, ts, date, "mixed_english_start")
            context = (
                "[Prompt Pre-processing]\n"
                f"Language detected: English (with Chinese parts)\n\n"
                f"{display}\n\n"
                f"(Saved to: {filepath})\n"
            )
        else:  # pure English
            display, filepath = process_english(prompt, ts, date, "english")
            context = (
                "[Prompt Pre-processing]\n"
                f"Language detected: English\n\n"
                f"{display}\n\n"
                f"(Saved to: {filepath})\n"
            )

        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }

    except Exception as exc:  # noqa: BLE001
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"[Prompt pre-processing error: {exc}]",
            }
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
