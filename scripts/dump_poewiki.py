#!/usr/bin/env python3
"""Dump current PoE Wiki pages to JSONL.

Each output line is:
{"pageid", "revid", "timestamp", "title", "wikitext"}
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://www.poewiki.net/w/api.php"
USER_AGENT = "poewiki-dataset-dump/1.0"

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "poewiki_pages.jsonl"
PAGE_BATCH_SIZE = 50
MAXLAG_SECONDS = 5
TIMEOUT_SECONDS = 60
RETRIES = 6

STRIP_SECTIONS = {
    "Item acquisition",
    "References",
    "External links",
    "Version history",
    "Recent changes",
    "Related items",
    "Location",
    "Legacy variants",
    "Gallery",
    "Alternate artwork",
    "See also",
    "Trivia",
    "Build of the Week",
    "Ruthless mode",
    "Related unique items",
    "Related passive skill",
    "Related passive skills",
    "Related modifiers",
    "Pantheon",
    "Miscellaneous low life benefits",
    "Base items",
    "Unique items",
    "Skill gems",
    "Support gems",
    "Footnotes",
    "Sources",
    "Passive skills",
}

STRIP_TEMPLATE_NAMES = [
    "Navbox",
    "Skill progression",
    "Alternate skill effect list",
    "Skill enchantment modifier list",
    "Item acquisition",
    "reflist",
    "Version history table",
    "Passive skill box",
    "status",
    "Drop enabled base item table",
    "Drop enabled unique item table",
    "Drop enabled item table",
    "Item skin list",
    "Item table",
    "legacy variant table header",
    "legacy variant table row",
    "Legacy variant table end",
    "Query base passive skills",
    "Query keystone passive skills",
    "Query ascendancy passive skills",
    "Query masteries",
    "Query base item table",
    "Query unique item table",
    "Query spawnable modifiers",
    "Modifier table",
    "Passive skill table",
    "Pantheon",
    "notelist",
    "#lst:",
    "Ascendancy Class",
    "#ev:youtube",
]

HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
REF_SELF_CLOSING_RE = re.compile(r"<ref\b[^>]*/\s*>", re.IGNORECASE)
REF_BLOCK_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.IGNORECASE | re.DOTALL)
GALLERY_RE = re.compile(r"<gallery.*?</gallery>", re.IGNORECASE | re.DOTALL)
FILE_RE = re.compile(r"\[\[File:(?:[^\[\]]|\[\[[^\]]*\]\])*\]\]", re.IGNORECASE)
SECTION_SPLIT_RE = re.compile(r"^(==[^=].*==)\s*$", re.MULTILINE)
BOLD_ITALIC_RE = re.compile(r"'{2,3}")
TOC_RE = re.compile(r"__(?:TOC|NOTOC|FORCETOC)__")
HRULE_RE = re.compile(r"^----+\s*$", re.MULTILINE)
SUBSECTION_HEADING_RE = re.compile(r"^={3,}\s*(.+?)\s*={3,}\s*$", re.MULTILINE)
WIKI_TABLE_RE = re.compile(r"\{\|[\s\S]*?\|\}", re.DOTALL)
HTML_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE | re.DOTALL)
WIKI_LINK_PIPE_RE = re.compile(r"\[\[([^|\]]+)\|([^\]]+)\]\]")
WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^==\s*(.+?)\s*==\s*$", re.MULTILINE)
SINGLE_BRACE_RE = re.compile(r"\{\{[^}]*\}\}", re.DOTALL)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
BARE_URL_RE = re.compile(r'https?://[^\s\]\)\}<>"]+')
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

STRIP_TEMPLATE_PATTERNS = [
    re.compile(
        r"\{\{" + re.escape(name) + ("" if name.endswith(":") else r"\b"),
        re.IGNORECASE,
    )
    for name in STRIP_TEMPLATE_NAMES
]


def fetch_json(params: dict[str, str]) -> dict:
    query = {
        **params,
        "format": "json",
        "formatversion": "2",
        "maxlag": str(MAXLAG_SECONDS),
    }
    url = API_URL + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as err:
            if err.code == 503:
                wait = retry_after_seconds(err) or MAXLAG_SECONDS + 1
            else:
                wait = 2**attempt
            print(f"HTTP {err.code}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
        ) as err:
            wait = 2**attempt
            print(f"{err!r}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        error = data.get("error")
        if error and error.get("code") == "maxlag":
            wait = int(float(error.get("lag", MAXLAG_SECONDS))) + 1
            print(f"maxlag; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if error:
            raise RuntimeError(f"API error: {error}")
        return data

    raise RuntimeError(f"gave up fetching {url}")


def retry_after_seconds(err: urllib.error.HTTPError) -> int | None:
    if not err.headers:
        return None
    value = err.headers.get("Retry-After")
    if value and value.isdigit():
        return int(value)
    return None


def cursor_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".cursor")


def load_cursor(output: Path) -> dict[str, str] | None:
    path = cursor_path(output)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in data.items()}


def save_cursor(output: Path, value: dict | None) -> None:
    path = cursor_path(output)
    if value is None:
        path.unlink(missing_ok=True)
        return
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp_path, path)


def summary_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".summary" + input_path.suffix)


def summarize_wikitext(wikitext: str) -> str:
    parts = []
    if item_fields := extract_item_template_fields(wikitext):
        parts.append(item_fields)
    if prose := extract_prose_sections(wikitext):
        parts.append(prose)

    result = "\n\n".join(parts)
    result = strip_ref_markup(result)
    result = BARE_URL_RE.sub("", result)
    result = BOLD_ITALIC_RE.sub("", result)
    result = TOC_RE.sub("", result)
    result = HRULE_RE.sub("", result)
    result = SUBSECTION_HEADING_RE.sub(lambda match: match.group(1), result)
    result = HEADING_RE.sub(lambda match: match.group(1), result)
    result = MULTI_NEWLINE_RE.sub("\n\n", result)
    return result.strip()


def add_summaries(input_path: Path) -> None:
    if not input_path.exists():
        raise RuntimeError(f"{input_path} does not exist")

    output_path = summary_output_path(input_path)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    written = 0
    with input_path.open(encoding="utf-8") as source, tmp_path.open("w", encoding="utf-8") as target:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise RuntimeError(f"{input_path}:{line_number} must contain a JSON object")
            wikitext = row.get("wikitext")
            if not isinstance(wikitext, str):
                raise RuntimeError(f"{input_path}:{line_number} is missing string field wikitext")
            row["summary_text"] = summarize_wikitext(wikitext)
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    os.replace(tmp_path, output_path)
    print(f"wrote {written} rows to {output_path}", file=sys.stderr)


def extract_item_block(wikitext: str) -> str:
    start = wikitext.find("{{Item")
    if start == -1:
        return ""
    return extract_balanced_template(wikitext, start)


def extract_balanced_template(wikitext: str, start_idx: int) -> str:
    depth = 0
    i = start_idx
    while i < len(wikitext) - 1:
        pair = wikitext[i : i + 2]
        if pair == "{{":
            depth += 1
            i += 2
        elif pair == "}}":
            depth -= 1
            if depth == 0:
                return wikitext[start_idx : i + 2]
            i += 2
        else:
            i += 1
    return ""


def get_field(block: str, name: str) -> str:
    needle = "|" + name
    for line in block.splitlines():
        trimmed = line.lstrip(" \t")
        if not trimmed.startswith(needle):
            continue
        rest = trimmed[len(needle) :].lstrip(" \t")
        if not rest or rest[0] != "=":
            continue
        return rest[1:].strip()
    return ""


def strip_br(text: str) -> str:
    return BR_RE.sub("\n", text)


def strip_ref_markup(text: str) -> str:
    text = REF_SELF_CLOSING_RE.sub("", text)
    return REF_BLOCK_RE.sub("", text)


def has_prose(text: str) -> bool:
    return len(SINGLE_BRACE_RE.sub("", text).strip()) > 0


def extract_item_template_fields(wikitext: str) -> str:
    block = extract_item_block(wikitext)
    if not block:
        return ""

    lines = []
    if tags := get_field(block, "gem_tags"):
        lines.append("Tags: " + tags)
    if desc := get_field(block, "gem_description"):
        lines.append(desc)
    if req_level := get_field(block, "required_level"):
        lines.append("Requires Level " + req_level)

    stat_parts = []
    if cast_time := get_field(block, "cast_time"):
        stat_parts.append("Cast time: " + cast_time + "s")
    if attack_time := get_field(block, "attack_time"):
        stat_parts.append("Attack time: " + attack_time + "s")
    if crit := get_field(block, "static_critical_strike_chance"):
        stat_parts.append("Base crit: " + crit + "%")
    if dmg_eff := get_field(block, "static_damage_effectiveness"):
        stat_parts.append("Effectiveness of Added Damage: " + dmg_eff + "%")
    if cost_types := get_field(block, "static_cost_types"):
        stat_parts.append("Cost: " + cost_types)
    if stat_parts:
        lines.append(" | ".join(stat_parts))

    if stat_text := get_field(block, "stat_text"):
        lines.append(strip_br(stat_text))
    if quality := get_field(block, "quality_type1_stat_text"):
        lines.append("Quality: " + strip_br(quality))

    if level20_text := get_field(block, "level20_stat_text"):
        level20_parts = [strip_br(level20_text)]
        if level20_eff := get_field(block, "level20_damage_effectiveness"):
            level20_parts.append(level20_eff + "% damage effectiveness")
        if level20_cost := get_field(block, "level20_cost_amounts"):
            level20_parts.append(level20_cost + " mana cost")
        lines.append("Level 20: " + ", ".join(level20_parts))

    if level21_text := get_field(block, "level21_stat_text"):
        level21_parts = [strip_br(level21_text)]
        if level21_eff := get_field(block, "level21_damage_effectiveness"):
            level21_parts.append(level21_eff + "% damage effectiveness")
        if level21_cost := get_field(block, "level21_cost_amounts"):
            level21_parts.append(level21_cost + " mana cost")
        lines.append("Level 21: " + ", ".join(level21_parts))

    return "\n".join(lines)


def extract_prose_sections(wikitext: str) -> str:
    item_block = extract_item_block(wikitext)
    text = wikitext
    if item_block:
        text = text.replace(item_block, "", 1)

    text = HTML_COMMENT_RE.sub("", text)
    text = strip_templates(text, STRIP_TEMPLATE_PATTERNS)
    text = WIKI_TABLE_RE.sub("", text)
    text = HTML_TABLE_RE.sub("", text)
    text = strip_ref_markup(text)
    text = GALLERY_RE.sub("", text)
    text = FILE_RE.sub("", text)
    text = normalize_inline_markup(text)

    headings = list(SECTION_SPLIT_RE.finditer(text))
    output = []
    preamble = text[: headings[0].start()].strip() if headings else text.strip()
    if has_prose(preamble):
        output.append(preamble)

    for idx, heading_match in enumerate(headings):
        heading = heading_match.group(1).strip()
        section_match = HEADING_RE.match(heading)
        if section_match is None:
            continue
        section_name = section_match.group(1)
        if section_name in STRIP_SECTIONS:
            continue
        body_start = heading_match.end()
        body_end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        body = text[body_start:body_end].strip()
        if has_prose(body):
            output.append(heading)
            output.append(body)

    return "\n\n".join(output)


def split_template_fields(body: str) -> list[str]:
    fields = []
    current = []
    brace_depth = 0
    link_depth = 0
    i = 0
    while i < len(body):
        pair = body[i : i + 2]
        if pair == "{{":
            brace_depth += 1
            current.append(pair)
            i += 2
        elif pair == "}}" and brace_depth > 0:
            brace_depth -= 1
            current.append(pair)
            i += 2
        elif pair == "[[":
            link_depth += 1
            current.append(pair)
            i += 2
        elif pair == "]]" and link_depth > 0:
            link_depth -= 1
            current.append(pair)
            i += 2
        elif body[i] == "|" and brace_depth == 0 and link_depth == 0:
            fields.append("".join(current))
            current = []
            i += 1
        else:
            current.append(body[i])
            i += 1
    fields.append("".join(current))
    return fields


def normalize_inline_markup(text: str) -> str:
    text = WIKI_LINK_PIPE_RE.sub(r"\2", text)
    text = WIKI_LINK_RE.sub(r"\1", text)
    text = resolve_remaining_templates(text)
    text = HTML_TAG_RE.sub("", text)
    text = BOLD_ITALIC_RE.sub("", text)
    return text


def resolve_remaining_templates(text: str) -> str:
    result = []
    i = 0
    while i < len(text):
        if text[i : i + 2] == "{{":
            end = find_balanced_close(text, i)
            if end >= 0:
                inner = text[i + 2 : end]
                result.append(pick_template_display(split_template_fields(inner)))
                i = end + 2
                continue
        result.append(text[i])
        i += 1
    return "".join(result)


def find_balanced_close(text: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(text) - 1:
        pair = text[i : i + 2]
        if pair == "{{":
            depth += 1
            i += 2
        elif pair == "}}":
            depth -= 1
            if depth == 0:
                return i
            i += 2
        else:
            i += 1
    return -1


def pick_template_display(fields: list[str]) -> str:
    if len(fields) <= 1:
        return ""
    for value in reversed(fields[1:]):
        value = value.strip()
        if value and "=" not in value:
            return value
    return ""


def strip_templates(text: str, patterns: list[re.Pattern[str]]) -> str:
    result = text
    for pattern in patterns:
        search_from = 0
        while True:
            match = pattern.search(result, search_from)
            if match is None:
                break
            start_idx = match.start()
            end_idx = -1
            depth = 0
            i = start_idx
            while i < len(result) - 1:
                pair = result[i : i + 2]
                if pair == "{{":
                    depth += 1
                    i += 2
                elif pair == "}}":
                    depth -= 1
                    if depth == 0:
                        end_idx = i + 2
                        break
                    i += 2
                else:
                    i += 1
            if end_idx > start_idx:
                result = result[:start_idx] + result[end_idx:]
                search_from = start_idx
            else:
                break
    return result


def page_record(page: dict) -> dict | None:
    revisions = page.get("revisions") or []
    if not revisions:
        return None
    revision = revisions[0]
    slot = revision.get("slots", {}).get("main", {})
    return {
        "pageid": page.get("pageid"),
        "revid": revision.get("revid"),
        "timestamp": revision.get("timestamp"),
        "title": page.get("title"),
        "wikitext": slot.get("content", ""),
    }


def dump_pages(output: Path, *, resume: bool, sleep_seconds: float, limit: int | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    continuation = load_cursor(output) if resume else None
    if resume and continuation is None:
        raise RuntimeError(f"--resume requested but no cursor exists at {cursor_path(output)}")
    target = output if resume else output.with_suffix(output.suffix + ".tmp")
    mode = "a" if resume and target.exists() else "w"
    written = 0
    if not resume:
        target.unlink(missing_ok=True)
        save_cursor(output, None)

    with target.open(mode, encoding="utf-8") as file:
        while True:
            batch_limit = PAGE_BATCH_SIZE
            if limit is not None:
                remaining = limit - written
                if remaining <= 0:
                    finish_dump(target, output, resume)
                    print(f"dumped {written} pages to {output}", file=sys.stderr)
                    return
                batch_limit = min(batch_limit, remaining)

            params = {
                "action": "query",
                "generator": "allpages",
                "gapnamespace": "0",
                "gapfilterredir": "nonredirects",
                "gaplimit": str(batch_limit),
                "prop": "revisions",
                "rvprop": "content|ids|timestamp",
                "rvslots": "main",
            }
            if continuation:
                params.update(continuation)

            data = fetch_json(params)
            next_continuation = data.get("continue")
            for page in data.get("query", {}).get("pages", []):
                record = page_record(page)
                if record is None:
                    continue
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                if limit is not None and written >= limit:
                    file.flush()
                    if resume:
                        save_cursor(output, next_continuation)
                    finish_dump(target, output, resume)
                    print(f"dumped {written} pages to {output}", file=sys.stderr)
                    return

            file.flush()
            continuation = next_continuation
            if resume:
                save_cursor(output, continuation)
            print(f"dumped {written} pages", file=sys.stderr)

            if not continuation:
                break
            time.sleep(sleep_seconds)

    finish_dump(target, output, resume)
    print(f"done: {output}", file=sys.stderr)


def finish_dump(target: Path, output: Path, resume: bool) -> None:
    if not resume:
        os.replace(target, output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump current PoE Wiki pages to JSONL.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true", help="append from the saved cursor")
    parser.add_argument(
        "--summary-only",
        type=Path,
        metavar="FILE",
        help="write FILE.summary.jsonl with summary_text added from existing wikitext",
    )
    parser.add_argument("--sleep", type=float, default=1.0, help="seconds between API batches")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many pages")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.summary_only is not None:
        if args.resume:
            raise RuntimeError("--summary-only cannot be combined with --resume")
        if args.limit is not None:
            raise RuntimeError("--summary-only cannot be combined with --limit")
        add_summaries(args.summary_only)
        return
    dump_pages(args.output, resume=args.resume, sleep_seconds=args.sleep, limit=args.limit)


if __name__ == "__main__":
    main()
