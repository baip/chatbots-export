#!/usr/bin/env python3
"""
Convert LibreChat JSON exports (for example from genai.umass.edu) to Markdown,
one file per conversation, in the same layout as chatgpt_json_to_md.py.

LibreChat exports one JSON file per chat (Share > Export, type json). Export with
"Include endpoint options", "Export all message branches" and "Recursive" checked
(librechat_export_json.js does this for you).

Usage:
  python3 librechat_json_to_md.py ~/Downloads/librechat            # every *.json in a folder
  python3 librechat_json_to_md.py a.json b.json -o my_chats --include-tools --timestamps

Options:
  -o, --out DIR        Output folder (default: <input folder>_md next to the input folder)
  --include-tools      Also include tool calls and their output (hidden by default)
  --include-thinking   Include the model's thinking as collapsible blocks
  --timestamps         Add a timestamp under each message heading
  --tz ZONE            Time zone for dates, e.g. America/New_York (default: your computer's local zone)
  --all-branches       Append the other branches of a conversation (default: only the thread
                       the site shows, which is the newest reply at each fork)
  --full-tool-output   Do not shorten long tool output (default: 3000 characters)
  --base-url URL       Site address used for the url field (default: https://genai.umass.edu)

If the same chat was exported more than once (for example an empty export and a later
complete one), the export with the most messages is used. Exports with no messages are
listed under "Not converted" in _index.md so you know which chats to export again.

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import json
import re
from pathlib import Path

from md_common import (Writer, attachments_line, base_parser, details, document, fence,
                       fmt_dt, message_block, set_timezone)

TOOL_OUTPUT_LIMIT = 3000


# ---------------------------------------------------------------- input loading
def load_exports(inputs):
    """Return {conversationId: (data, path)}, keeping the fullest export of each chat."""
    files = []
    for i in inputs:
        p = Path(i).expanduser()
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    best, unreadable = {}, []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            unreadable.append((f.name, str(e)))
            continue
        if not isinstance(data, dict) or "conversationId" not in data:
            continue  # not a LibreChat export
        cid = data["conversationId"]
        n = count_messages(data.get("messages") or [])
        if cid not in best or n > best[cid][2]:
            best[cid] = (data, f, n)
    return {k: (d, f) for k, (d, f, _) in best.items()}, unreadable


def count_messages(nodes):
    return sum(1 + count_messages(n.get("children") or []) for n in nodes)


def all_messages(nodes):
    for n in nodes:
        yield n
        yield from all_messages(n.get("children") or [])


# ---------------------------------------------------------------- message ordering
def newest_first_path(siblings, forks, depth=0):
    """Follow the newest reply at each fork (what the site shows by default).

    Records the skipped siblings in forks as (position, node) for --all-branches.
    """
    path = []
    while siblings:
        siblings = sorted(siblings, key=lambda n: n.get("createdAt") or "")
        for other in siblings[:-1]:
            forks.append((depth + len(path), other))
        path.append(siblings[-1])
        siblings = siblings[-1].get("children") or []
    return path


# ---------------------------------------------------------------- content rendering
def sender_label(m):
    if m.get("isCreatedByUser"):
        return "User"
    name = m.get("sender") or m.get("model") or "Assistant"
    name = re.sub(r"[^\w\s.\-:()/+&,']+", "", name).strip()  # drop the site's capability icons
    return name or "Assistant"


def pretty(value):
    """Pretty-print JSON (given as a string or object) and return (text, fence language)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False), "json"
    if not isinstance(value, str):
        return str(value), ""
    try:
        return json.dumps(json.loads(value), indent=2, ensure_ascii=False), "json"
    except (ValueError, TypeError):
        return value, ""


def render_tool_call(tc, args):
    text, lang = pretty(tc.get("args", ""))
    body = f"**Arguments**\n\n{fence(text, lang)}"
    out = tc.get("output")
    if out not in (None, ""):
        text, lang = pretty(out)
        if not args.full_tool_output and len(text) > TOOL_OUTPUT_LIMIT:
            text = text[:TOOL_OUTPUT_LIMIT] + f"\n... [shortened, {len(text):,} characters in total]"
            lang = ""
        body += f"\n\n**Output**\n\n{fence(text, lang)}"
    return details(f"Tool call: {tc.get('name') or 'tool'}", body)


def render_part(p, args):
    t = p.get("type")
    if t == "text":
        v = p.get("text")
        return (v.get("value", "") if isinstance(v, dict) else v or "").strip()
    if t == "think":
        return details("Thinking", (p.get("think") or "").strip()) if args.include_thinking else ""
    if t == "tool_call":
        return render_tool_call(p.get("tool_call") or {}, args) if args.include_tools else ""
    if t in ("image_file", "image_url"):
        v = p.get(t) or {}
        return f"*[Image: {v.get('url') or v.get('filepath') or v.get('file_id') or 'image'}]*"
    if t == "error":
        return f"*[Error: {p.get('error') or p.get('text') or ''}]*"
    # Unknown type: keep any text we can find.
    for k in ("text", "content"):
        if isinstance(p.get(k), str) and p[k].strip():
            return p[k]
    return ""


def render_content(m, args):
    content = m.get("content")
    if content:
        parts = [render_part(p, args) for p in content if isinstance(p, dict)]
    else:
        parts = [(m.get("text") or "").strip()]
    if m.get("error"):
        parts.append("*[This message ended with an error.]*")
    elif m.get("unfinished"):
        parts.append("*[This message is unfinished.]*")
    return "\n\n".join(x for x in parts if x and x.strip())


def message_attachments(m):
    names = [f.get("filename") or f.get("file_id") for f in (m.get("files") or []) if isinstance(f, dict)]
    names += [a.get("filename") or a.get("file_id") for a in (m.get("attachments") or []) if isinstance(a, dict)]
    return attachments_line(names)


def render_thread(path, args):
    """Message blocks for one thread, merging consecutive assistant messages like the ChatGPT converter."""
    out, n, last_role = [], 0, None
    for m in path:
        body, att = render_content(m, args), message_attachments(m)
        if not body and not att:
            continue
        role = "user" if m.get("isCreatedByUser") else "assistant"
        if role == last_role == "assistant" and not args.timestamps and not att:
            out.append(body)
        else:
            out.append(message_block(sender_label(m), body, m.get("createdAt"), att, args.timestamps))
        last_role = role
        n += 1
    return out, n


# ---------------------------------------------------------------- conversation -> markdown
def convert(data, args):
    title = data.get("title") or "Untitled"
    cid = data.get("conversationId") or ""
    roots = data.get("messages") or []
    every = list(all_messages(roots))
    created = min((m.get("createdAt") for m in every if m.get("createdAt")), default=None)
    updated = max((m.get("updatedAt") or m.get("createdAt") for m in every
                   if m.get("updatedAt") or m.get("createdAt")), default=None)
    o = data.get("options") or {}

    forks = []
    out, n_msgs = render_thread(newest_first_path(roots, forks), args)

    if args.all_branches and forks:
        out.append("---\n\n# Other branches")
        k = 0
        while forks:
            pos, node = forks.pop(0)
            k += 1
            sub_forks = []
            blocks, _ = render_thread(newest_first_path([node], sub_forks, pos), args)
            forks += sub_forks
            where = f"after message {pos}" if pos else "at the start"
            out.append(f"## Branch {k} (splits off {where})")
            out += ["#" + b if b.startswith("## ") else b for b in blocks]

    front = [
        ("title", title),
        ("id", cid),
        ("url", f"{args.base_url.rstrip('/')}/c/{cid}" if cid else ""),
        ("created", fmt_dt(created)),
        ("updated", fmt_dt(updated)),
        ("model", o.get("model") or o.get("spec") or ""),
        ("agent", o.get("agent_id") or ""),
        ("endpoint", data.get("endpoint") or o.get("endpoint") or ""),
    ]
    return document(title, front, out), n_msgs, title, cid, created


# ---------------------------------------------------------------- main
def main():
    ap = base_parser("Convert LibreChat JSON exports to Markdown files.",
                     "Export JSON files, or folders containing them", multiple_inputs=True)
    ap.add_argument("--all-branches", action="store_true", help="Append the other branches of each chat")
    ap.add_argument("--full-tool-output", action="store_true", help="Do not shorten long tool output")
    ap.add_argument("--base-url", default="https://genai.umass.edu", help="Site address for the url field")
    args = ap.parse_args()
    set_timezone(args.tz)

    first = Path(args.inputs[0]).expanduser()
    folder = first if first.is_dir() else first.parent
    writer = Writer(Path(args.out) if args.out else folder.with_name(folder.name + "_md"),
                    "LibreChat conversations")

    exports, unreadable = load_exports(args.inputs)
    for name, err in unreadable:
        writer.fail(name, "", f"could not read: {err}")
    for cid, (data, path) in exports.items():
        title = data.get("title") or path.stem
        if not data.get("messages"):
            writer.fail(title, cid, "export has no messages; export this chat again")
            continue
        try:
            md, n, title, cid, created = convert(data, args)
        except Exception as e:  # keep going on one bad conversation
            writer.fail(title, cid, f"conversion error: {e}")
            continue
        writer.add(md, title, cid, created, n)
    writer.finish()


if __name__ == "__main__":
    main()
