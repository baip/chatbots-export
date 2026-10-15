#!/usr/bin/env python3
"""
Convert a Perplexity data export to Markdown, one file per thread, in the same layout
as chatgpt_json_to_md.py.

Perplexity (Settings > Account > Export data) emails a zip,
user_data_export_<date>_<id>.zip, that holds conversations-<date>.json and a
spreadsheet of account settings. Pass either the zip or the JSON file:
  {"conversations": [{context_uuid, context_title, created_at, updated_at, mode,
                      collection_uuid, entries: [{query, answer, created_at,
                      engine_mode, query_status, ...}]}]}

Usage:
  python3 perplexity_json_to_md.py conversations-20261015_032127-c961e618.json
  python3 perplexity_json_to_md.py user_data_export_2026-10-15-....zip --strip-citations

Options:
  -o, --out DIR        Output folder (default: <input name>_md next to the input file)
  --timestamps         Add a timestamp under each message heading
  --tz ZONE            Time zone for dates, e.g. America/New_York (default: your computer's local zone)
  --strip-citations    Remove the [1], [2] citation markers. The export does not include the
                       sources they point to, so they cannot be turned into links.
                       Code blocks and inline code are left alone.

The export has no thread links (the url field is left blank) and no sources, images or
attachments. Thread titles are the first question when Perplexity did not name the
thread, so long ones are shortened. Queries that Perplexity blocked are marked as such.

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import json
import re
import zipfile
from pathlib import Path

from md_common import (Writer, base_parser, document, fmt_dt, message_block, set_timezone,
                       short_title, sort_key)

CITATION = re.compile(r"[ \t]*(?:\[\d{1,3}\])+(?![(\[])")
CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)


# ---------------------------------------------------------------- input loading
def load_export(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json")]
            names.sort(key=lambda n: (not Path(n).name.startswith("conversations"), n))
            if not names:
                raise SystemExit(f"No JSON file found in {path}.")
            data = json.loads(z.read(names[0]).decode("utf-8"))
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        return data["conversations"]
    if isinstance(data, list):
        return data
    raise SystemExit("Unrecognized JSON layout: expected a 'conversations' list.")


# ---------------------------------------------------------------- rendering
def strip_citations(text):
    parts = CODE.split(text)
    return "".join(p if i % 2 else CITATION.sub("", p) for i, p in enumerate(parts))


def render_answer(e, args):
    answer = (e.get("answer") or "").strip()
    status = (e.get("query_status") or "").upper()
    if args.strip_citations:
        answer = strip_citations(answer)
    notes = []
    if status == "BLOCKED":
        notes.append("*[Perplexity blocked this query.]*")
    elif status and status != "COMPLETED":
        notes.append(f"*[Query status: {status.lower()}]*")
    if not answer and not notes:
        notes.append("*[No answer in the export.]*")
    return "\n\n".join(x for x in [answer] + notes if x)


def convert(conv, args):
    entries = sorted((e for e in conv.get("entries") or [] if isinstance(e, dict)),
                     key=lambda e: sort_key(e.get("created_at")))
    first_query = next((e.get("query") for e in entries if (e.get("query") or "").strip()), "")
    title = short_title(conv.get("context_title") or first_query or "Untitled")
    cid = conv.get("context_uuid") or ""

    blocks, n = [], 0
    for e in entries:
        q = (e.get("query") or "").strip() or "*[Empty query]*"
        blocks.append(message_block("User", q, e.get("created_at"), "", args.timestamps))
        blocks.append(message_block("Perplexity", render_answer(e, args), e.get("created_at"), "", args.timestamps))
        n += 2

    engines = []
    for e in entries:
        m = e.get("engine_mode")
        if m and m not in engines:
            engines.append(m)
    front = [
        ("title", title),
        ("id", cid),
        ("url", ""),
        ("created", fmt_dt(conv.get("created_at"))),
        ("updated", fmt_dt(conv.get("updated_at"))),
        ("mode", conv.get("mode") or ""),
        ("engine", ", ".join(engines)),
        ("space", conv.get("collection_uuid") or ""),
    ]
    return document(title, front, blocks), n, title, cid, conv.get("created_at")


# ---------------------------------------------------------------- main
def main():
    ap = base_parser("Convert a Perplexity data export to Markdown files.",
                     "The export zip, or the conversations-*.json inside it", tool_options=False)
    ap.add_argument("--strip-citations", action="store_true",
                    help="Remove [1]-style citation markers (the export has no sources for them)")
    args = ap.parse_args()
    set_timezone(args.tz)

    src = Path(args.input).expanduser()
    writer = Writer(Path(args.out) if args.out else src.with_name(src.stem + "_md"), "Perplexity threads")
    for conv in load_export(src):
        if not isinstance(conv, dict):
            continue
        title = short_title(conv.get("context_title") or "Untitled")
        cid = conv.get("context_uuid") or ""
        if not conv.get("entries"):
            writer.fail(title, cid, "thread has no entries")
            continue
        try:
            md, n, title, cid, created = convert(conv, args)
        except Exception as e:  # keep going on one bad thread
            writer.fail(title, cid, f"conversion error: {e}")
            continue
        writer.add(md, title, cid, created, n)
    writer.finish()


if __name__ == "__main__":
    main()
