#!/usr/bin/env python3
"""
Convert a ChatGPT JSON export to Markdown, one file per conversation.

Works with:
  * chatgpt-export-YYYY-MM-DD.json from github.com/hoya98/chatgpt-export
    (top-level {"conversations": [{id, title, create_time, update_time, conversation: {...}}]})
  * conversations.json from ChatGPT's built-in "Export data" (a list of conversation objects)

Usage:
  python3 chatgpt_json_to_md.py chatgpt-export-2026-10-14.json
  python3 chatgpt_json_to_md.py export.json -o my_chats --include-tools --timestamps

Options:
  -o, --out DIR        Output folder (default: <input name>_md next to the input file)
  --include-tools      Also include tool/browsing/code-execution output (hidden by default)
  --include-thinking   Include reasoning summaries ("thoughts") as collapsible blocks
  --timestamps         Add a timestamp under each message heading
  --tz ZONE            Time zone for dates, e.g. America/New_York (default: your computer's local zone)

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import json
import re
from pathlib import Path

from md_common import (Writer, attachments_line, base_parser, details, document, fence,
                       fmt_dt, message_block, set_timezone)

PRIVATE_USE = re.compile("[-]")  # citation markers ChatGPT embeds in text


# ---------------------------------------------------------------- input loading
def load_conversations(data):
    """Yield (wrapper_meta, conversation_obj_or_None, error_or_None)."""
    if isinstance(data, dict) and "conversations" in data:
        items = data["conversations"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "mapping" in data:
        items = [data]
    else:
        raise SystemExit("Unrecognized JSON layout: expected a 'conversations' list or a list of conversations.")
    for it in items:
        if not isinstance(it, dict):
            continue
        if "conversation" in it or "error" in it and "mapping" not in it:
            conv = it.get("conversation")
            yield it, conv if isinstance(conv, dict) else None, it.get("error")
        else:  # official export: the item is the conversation itself
            yield it, it, None


# ---------------------------------------------------------------- message ordering
def active_path(mapping, current_node):
    """Messages on the branch the user last saw (follows parents up from current_node)."""
    if not mapping:
        return []
    node_id = current_node if current_node in mapping else None
    if node_id is None:
        # Fall back to the most recent leaf.
        leaves = [k for k, v in mapping.items() if not v.get("children")]
        def t(k):
            m = (mapping[k] or {}).get("message") or {}
            return m.get("create_time") or 0
        node_id = max(leaves, key=t) if leaves else None
    path, seen = [], set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        path.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    return list(reversed(path))


# ---------------------------------------------------------------- content rendering
def citation_replacement(ref):
    rep = ""
    links = []
    for it in ref.get("items") or []:
        url, title = it.get("url"), it.get("title") or it.get("attribution")
        if url:
            links.append(f"[{title or url}]({url})")
    if links:
        rep = "(" + "; ".join(links) + ")"
    elif ref.get("alt"):
        rep = ref["alt"]
    elif ref.get("safe_urls"):
        rep = "(" + "; ".join(f"<{u}>" for u in ref["safe_urls"]) + ")"
    return rep


def apply_citations(text, meta):
    """Swap ChatGPT's citation markers for links.

    Some references (for example source footnotes) have matched_text " ", a single space.
    Replacing those globally deleted every space in the message, so whitespace-only
    references are skipped, and references are applied at their own positions only.
    """
    refs = [r for r in ((meta or {}).get("content_references") or [])
            if isinstance(r, dict) and (r.get("matched_text") or "").strip()]

    # 1) Position-based replacement, from the end so earlier offsets stay valid.
    positioned, rest = [], []
    for r in refs:
        s, e, mt = r.get("start_idx"), r.get("end_idx"), r["matched_text"]
        if isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text) and text[s:e] == mt:
            positioned.append(r)
        else:
            rest.append(r)
    last = len(text) + 1
    for r in sorted(positioned, key=lambda r: r["start_idx"], reverse=True):
        if r["end_idx"] > last:  # overlapping reference, handle by text below
            rest.append(r)
            continue
        text = text[: r["start_idx"]] + citation_replacement(r) + text[r["end_idx"]:]
        last = r["start_idx"]

    # 2) Text-based fallback, only for markers that contain ChatGPT's private-use characters,
    #    so ordinary words in the message are never touched.
    for ref in sorted(rest, key=lambda r: -len(r["matched_text"])):
        mt = ref["matched_text"]
        if not PRIVATE_USE.search(mt) or mt not in text:
            continue
        text = text.replace(mt, citation_replacement(ref))
    return PRIVATE_USE.sub("", text)


def render_part(p):
    if isinstance(p, str):
        return p
    if not isinstance(p, dict):
        return ""
    ct = p.get("content_type", "")
    if ct == "image_asset_pointer":
        ptr = p.get("asset_pointer", "")
        return f"*[Image: {ptr.split('://')[-1] or 'image'}]*"
    if ct == "audio_transcription":
        return p.get("text", "")
    if ct in ("audio_asset_pointer", "real_time_user_audio_video_asset_pointer"):
        return "*[Audio]*"
    if "text" in p and isinstance(p["text"], str):
        return p["text"]
    return f"*[{ct or 'attachment'}]*"


def render_content(msg, include_thinking):
    c = msg.get("content") or {}
    meta = msg.get("metadata") or {}
    ct = c.get("content_type")
    if ct in ("text", "multimodal_text"):
        parts = [render_part(p) for p in c.get("parts") or []]
        return apply_citations("\n\n".join(x for x in parts if x and x.strip()), meta)
    if ct == "code":
        lang = c.get("language") or ""
        if lang in ("unknown", "json") and msg.get("recipient") not in (None, "all", "python"):
            lang = "json"
        return fence(c.get("text", ""), "" if lang == "unknown" else lang)
    if ct == "execution_output":
        return fence(c.get("text", ""), "text")
    if ct == "tether_quote":
        title, url, text = c.get("title", ""), c.get("url", ""), c.get("text", "")
        head = f"[{title or url}]({url})" if url else title
        quoted = "\n".join("> " + ln for ln in text.splitlines())
        return f"{head}\n\n{quoted}".strip()
    if ct == "tether_browsing_display":
        return c.get("result", "") or c.get("summary", "") or ""
    if ct == "thoughts":
        if not include_thinking:
            return ""
        blocks = []
        for t in c.get("thoughts") or []:
            s, body = t.get("summary", ""), t.get("content", "")
            blocks.append(f"**{s}**\n\n{body}" if s else body)
        return details("Thinking", "\n\n".join(b for b in blocks if b.strip()))
    if ct == "reasoning_recap":
        return f"*{c.get('content', '')}*" if include_thinking and c.get("content") else ""
    if ct == "system_error":
        return f"*[Error: {c.get('name', '')} {c.get('text', '')}]*".strip()
    if ct in ("user_editable_context", "model_editable_context"):
        return ""  # custom instructions / memory, not part of the chat
    # Unknown type: keep any text we can find.
    for k in ("text", "result", "content"):
        if isinstance(c.get(k), str) and c[k].strip():
            return c[k]
    if c.get("parts"):
        return "\n\n".join(render_part(p) for p in c["parts"])
    return ""


def message_attachments(meta):
    atts = (meta or {}).get("attachments") or []
    return attachments_line(a.get("name") or a.get("id") for a in atts if isinstance(a, dict))


# ---------------------------------------------------------------- conversation -> markdown
ROLE_LABEL = {"user": "User", "assistant": "ChatGPT", "tool": "Tool", "system": "System"}


def convert(wrapper, conv, args):
    title = wrapper.get("title") or conv.get("title") or "Untitled"
    cid = wrapper.get("id") or conv.get("conversation_id") or conv.get("id") or ""
    created = wrapper.get("create_time") or conv.get("create_time")
    updated = wrapper.get("update_time") or conv.get("update_time")
    nodes = active_path(conv.get("mapping") or {}, conv.get("current_node"))

    out, model, n_msgs = [], conv.get("default_model_slug") or "", 0
    last_role = None
    for node in nodes:
        msg = node.get("message")
        if not msg:
            continue
        meta = msg.get("metadata") or {}
        role = (msg.get("author") or {}).get("role", "")
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        if role == "system":
            continue
        is_tool_step = role == "tool" or (role == "assistant" and msg.get("recipient") not in (None, "all"))
        if is_tool_step and not args.include_tools:
            continue
        body = render_content(msg, args.include_thinking).strip()
        att = message_attachments(meta)
        if not body and not att:
            continue
        model = model or meta.get("model_slug") or ""
        label = ROLE_LABEL.get(role, role.title() or "Message")
        if role == "tool":
            name = (msg.get("author") or {}).get("name")
            label = f"Tool ({name})" if name else "Tool"
        # Merge consecutive assistant chunks under one heading.
        if role == last_role == "assistant" and not args.timestamps:
            out.append(body)
        else:
            out.append(message_block(label, body, msg.get("create_time"), att, args.timestamps))
        last_role = role
        n_msgs += 1

    front = [
        ("title", title),
        ("id", cid),
        ("url", "https://chatgpt.com/c/" + cid if cid else ""),
        ("created", fmt_dt(created)),
        ("updated", fmt_dt(updated)),
        ("model", model),
    ]
    return document(title, front, out), n_msgs, title, cid, created


# ---------------------------------------------------------------- main
def main():
    ap = base_parser("Convert a ChatGPT JSON export to Markdown files.", "Path to the export JSON file")
    args = ap.parse_args()
    set_timezone(args.tz)

    src = Path(args.input).expanduser()
    with src.open(encoding="utf-8") as f:
        data = json.load(f)
    writer = Writer(Path(args.out) if args.out else src.with_name(src.stem + "_md"), "ChatGPT conversations")

    for wrapper, conv, err in load_conversations(data):
        title = wrapper.get("title") or "Untitled"
        if conv is None or not conv.get("mapping"):
            writer.fail(title, wrapper.get("id", ""), err or "no conversation data")
            continue
        try:
            md, n, title, cid, created = convert(wrapper, conv, args)
        except Exception as e:  # keep going on one bad conversation
            writer.fail(title, wrapper.get("id", ""), f"conversion error: {e}")
            continue
        writer.add(md, title, cid, created, n)
    writer.finish()


if __name__ == "__main__":
    main()
