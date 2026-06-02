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
    parser.add_argument("--sleep", type=float, default=1.0, help="seconds between API batches")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many pages")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dump_pages(args.output, resume=args.resume, sleep_seconds=args.sleep, limit=args.limit)


if __name__ == "__main__":
    main()
