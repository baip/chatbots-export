#!/usr/bin/env python3
"""
Convert a ChatGPT export to Markdown, one file per conversation.

Works with:
  * The official export (Settings > Data controls > Export data). Pass the emailed zip,
    chatgpt-<...>.zip, or the folder it was extracted to. It holds
      conversations.json, or shards conversations-000.json, conversations-001.json, ...
      conversation_asset_file_names.json   original names of the uploaded files
      file-*.dat, file_*.dat               uploaded and generated files (images, PDFs, ...)
      group_chats.json                     group chats
    You can also pass conversations.json or one shard; the other shards beside it are read too.
  * chatgpt-export-YYYY-MM-DD.json from chatgpt_export.js (github.com/hoya98/chatgpt-export):
    {"conversations": [{id, title, create_time, update_time, conversation: {...}}]}
    A zstd-compressed copy (.json.zst) also works if the zstd command is installed.

Usage:
  python3 chatgpt_json_to_md.py chatgpt-XDD-....zip
  python3 chatgpt_json_to_md.py chatgpt-export-2026-10-14.json
  python3 chatgpt_json_to_md.py export.json -o my_chats --include-tools --timestamps

Options:
  -o, --out DIR        Output folder (default: <input name>_md next to the input)
  --include-tools      Also include tool/browsing/code-execution output (hidden by default)
  --include-thinking   Include reasoning summaries ("thoughts") as collapsible blocks
  --timestamps         Add a timestamp under each message heading
  --tz ZONE            Time zone for dates, e.g. America/New_York (default: your computer's local zone)

Files in the official export (images and attachments) are linked from the messages that
use them, and images are shown inline. When the input is a zip, the files used are
extracted into a files/ subfolder of the output, so the output folder is self-contained.
When the input is a folder or a JSON file, they link to the originals with relative paths,
which break if either folder is moved. The official export leaves out tool calls, so
--include-tools only matters for chatgpt_export.js exports.

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from md_common import (Writer, base_parser, details, document, fence, fmt_dt, message_block,
                       set_timezone, short_title)

PRIVATE_USE = re.compile("[-]")  # citation markers ChatGPT embeds in text
SHARD = re.compile(r"^conversations(?:-(\d+))?\.json$", re.I)
ASSET = re.compile(r"^(file[-_][A-Za-z0-9]+)\.dat$")

MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv",
    "audio/wav": ".wav", "audio/mpeg": ".mp3",
}
MAGIC = [(b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"), (b"%PDF", ".pdf"),
         (b"RIFF", ".webp"), (b"PK\x03\x04", ".zip")]


# ---------------------------------------------------------------- input loading
def read_json_file(path):
    path = Path(path)
    if path.suffix.lower() == ".zst":
        try:
            raw = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            raise SystemExit(f"Could not decompress {path.name} (is zstd installed?): {e}")
        return json.loads(raw.decode("utf-8"))
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class Export:
    """Reads the official export (zip, folder or JSON shard) or a chatgpt_export.js JSON file."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.zip = None
        self.root = None          # folder holding the export files (folder input)
        self.members = {}         # name relative to the export root -> zip member or file path
        self.data = None          # parsed single JSON (chatgpt_export.js or a lone file)

        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self.zip = zipfile.ZipFile(self.path)
            names = [n for n in self.zip.namelist() if not n.endswith("/")]
            prefix = self._prefix(names)
            for n in names:
                if n.startswith(prefix):
                    self.members[n[len(prefix):]] = n
        elif self.path.is_dir():
            hits = sorted(self.path.rglob("conversations*.json"), key=lambda p: len(p.parts))
            self.root = hits[0].parent if hits else self.path
            for p in self.root.rglob("*"):
                if p.is_file():
                    self.members[p.relative_to(self.root).as_posix()] = p
        elif self.path.is_file() and SHARD.match(self.path.name):
            self.root = self.path.parent
            for p in self.root.iterdir():
                if p.is_file():
                    self.members[p.name] = p
        elif self.path.is_file():
            self.data = read_json_file(self.path)
        else:
            raise SystemExit(f"Not found: {self.path}")

        self.asset_names = {}
        if "conversation_asset_file_names.json" in self.members:
            self.asset_names = self.read_json("conversation_asset_file_names.json") or {}
        self.assets = {}          # file id -> member key (*.dat)
        for k in self.members:
            m = ASSET.match(k)
            if m:
                self.assets[m.group(1)] = k

    @staticmethod
    def _prefix(names):
        for n in names:
            if SHARD.match(PurePosixPath(n).name):
                parent = str(PurePosixPath(n).parent)
                return "" if parent == "." else parent + "/"
        return ""

    def read_bytes(self, key):
        m = self.members[key]
        return self.zip.read(m) if self.zip else Path(m).read_bytes()

    def read_json(self, key):
        return json.loads(self.read_bytes(key).decode("utf-8"))

    def conversations(self):
        if self.data is not None:
            return self.data
        shards = sorted((k for k in self.members if SHARD.match(k)),
                        key=lambda k: int(SHARD.match(k).group(1) or -1))
        if not shards:
            raise SystemExit(f"No conversations.json or conversations-NNN.json found in {self.path}.")
        convs = []
        for k in shards:
            part = self.read_json(k)
            convs.extend(part if isinstance(part, list) else part.get("conversations", []))
        return convs

    def group_chats(self):
        if "group_chats.json" not in self.members:
            return []
        g = self.read_json("group_chats.json")
        return g.get("chats", []) if isinstance(g, dict) else (g or [])

    # ---------------- files
    def file_name(self, fid, hint=None, mime=None):
        """Readable file name for an asset id, keeping the id so names stay unique."""
        key = self.assets.get(fid)
        name = self.asset_names.get(key or "", "") or hint or ""
        ext = os.path.splitext(name)[1]
        if not ext:
            ext = MIME_EXT.get(mime or "", "")
            if not ext and key:
                head = self.read_bytes(key)[:12]
                ext = next((e for sig, e in MAGIC if head.startswith(sig)), "")
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.splitext(name)[0]).strip() or "file"
        return f"{fid[:24]}_{stem}{ext}"

    def link(self, fid, out_dir, hint=None, mime=None):
        """Relative link to the file, extracting it into files/ for zip input. None if absent."""
        key = self.assets.get(fid)
        if not key:
            return None
        if self.zip:
            name = self.file_name(fid, hint, mime)
            dest = Path(out_dir) / "files" / name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with self.zip.open(self.members[key]) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            return "files/" + quote(name)
        return quote(os.path.relpath(self.members[key], out_dir).replace(os.sep, "/"))


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
        if "conversation" in it or ("error" in it and "mapping" not in it):
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


class Renderer:
    """Turns message content into Markdown; links export files when they are available."""

    def __init__(self, export, out_dir, args):
        self.export, self.out_dir, self.args = export, out_dir, args
        self.shown = set()  # file ids already shown inline in the current message

    def file_link(self, fid, hint=None, mime=None):
        return self.export.link(fid, self.out_dir, hint, mime) if self.export and fid else None

    def render_part(self, p):
        if isinstance(p, str):
            return p
        if not isinstance(p, dict):
            return ""
        ct = p.get("content_type", "")
        if ct == "image_asset_pointer":
            fid = (p.get("asset_pointer") or "").split("://")[-1]
            target = self.file_link(fid)
            if target:
                self.shown.add(fid)
                alt = self.export.asset_names.get(self.export.assets.get(fid, ""), "") or "image"
                return f"![{alt}]({target})"
            return f"*[Image: {fid or 'image'}]*"
        if ct == "audio_transcription":
            return p.get("text", "")
        if ct in ("audio_asset_pointer", "real_time_user_audio_video_asset_pointer"):
            return "*[Audio]*"
        if "text" in p and isinstance(p["text"], str):
            return p["text"]
        return f"*[{ct or 'attachment'}]*"

    def content(self, msg):
        c = msg.get("content") or {}
        meta = msg.get("metadata") or {}
        ct = c.get("content_type")
        inc_think = self.args.include_thinking
        if ct in ("text", "multimodal_text"):
            parts = [self.render_part(p) for p in c.get("parts") or []]
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
            if not inc_think:
                return ""
            blocks = []
            for t in c.get("thoughts") or []:
                s, body = t.get("summary", ""), t.get("content", "")
                blocks.append(f"**{s}**\n\n{body}" if s else body)
            return details("Thinking", "\n\n".join(b for b in blocks if b.strip()))
        if ct == "reasoning_recap":
            return f"*{c.get('content', '')}*" if inc_think and c.get("content") else ""
        if ct == "system_error":
            return f"*[Error: {c.get('name', '')} {c.get('text', '')}]*".strip()
        if ct in ("user_editable_context", "model_editable_context"):
            return ""  # custom instructions / memory, not part of the chat
        # Unknown type: keep any text we can find.
        for k in ("text", "result", "content"):
            if isinstance(c.get(k), str) and c[k].strip():
                return c[k]
        if c.get("parts"):
            return "\n\n".join(self.render_part(p) for p in c["parts"])
        return ""

    def attachments(self, atts):
        """'Attachments:' line; names link to the files when the export has them."""
        items = []
        for a in atts or []:
            if not isinstance(a, dict):
                continue
            fid, name = a.get("id") or a.get("file_id"), a.get("name") or a.get("file_name")
            if fid in self.shown:  # already shown inline as an image
                continue
            label = name or fid
            if not label:
                continue
            target = self.file_link(fid, name, a.get("mime_type"))
            if target and (a.get("mime_type") or "").startswith("image/"):
                items.append(f"![{label}]({target})")
            elif target:
                items.append(f"[{label}]({target})")
            else:
                items.append(f"`{label}`")
        if not items:
            return ""
        images = [i for i in items if i.startswith("![")]
        others = [i for i in items if not i.startswith("![")]
        line = ("*Attachments: " + ", ".join(others) + "*") if others else ""
        return "\n\n".join(x for x in [line] + images if x)


# ---------------------------------------------------------------- conversation -> markdown
ROLE_LABEL = {"user": "User", "assistant": "ChatGPT", "tool": "Tool", "system": "System"}


def convert(wrapper, conv, args, render):
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
        render.shown = set()
        body = render.content(msg).strip()
        att = render.attachments(meta.get("attachments"))
        if not body and not att:
            continue
        model = model or meta.get("model_slug") or ""
        label = ROLE_LABEL.get(role, role.title() or "Message")
        if role == "tool":
            name = (msg.get("author") or {}).get("name")
            label = f"Tool ({name})" if name else "Tool"
        # Merge consecutive assistant chunks under one heading.
        if role == last_role == "assistant" and not args.timestamps and not att:
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
        ("archived", "true" if conv.get("is_archived") else ""),
    ]
    return document(title, front, out), n_msgs, title, cid, created


def convert_group_chat(chat, args, render):
    msgs = sorted(chat.get("messages") or [], key=lambda m: m.get("created_at") or "")
    first_user = next((m.get("text") for m in msgs if m.get("role") == "user" and m.get("text")), "")
    title = chat.get("name") or short_title(first_user) or "Group chat"
    assistant = chat.get("assistant_name") or "ChatGPT"
    out, n = [], 0
    for m in msgs:
        render.shown = set()
        body = (m.get("text") or "").strip()
        att = render.attachments(m.get("attachments"))
        if not body and not att:
            continue
        role = m.get("role", "")
        label = assistant if role == "assistant" else ROLE_LABEL.get(role, role.title() or "Message")
        out.append(message_block(label, body, m.get("created_at"), att, args.timestamps))
        n += 1
    front = [
        ("title", title),
        ("id", chat.get("id", "")),
        ("url", ""),
        ("created", fmt_dt(chat.get("created_at"))),
        ("updated", fmt_dt(chat.get("updated_at") or chat.get("last_action_at"))),
        ("type", "group chat"),
        ("members", str(len(chat.get("members") or [])) if chat.get("members") else ""),
    ]
    return document(title, front, out), n, title, chat.get("id", ""), chat.get("created_at")


# ---------------------------------------------------------------- main
def default_out(src):
    name = src.name
    for suf in (".zst", ".json", ".zip"):
        if name.lower().endswith(suf):
            name = name[: -len(suf)]
    if src.is_dir():
        return src.with_name(src.name + "_md")
    return src.with_name(name + "_md")


def main():
    ap = base_parser("Convert a ChatGPT export (official zip/folder or chatgpt_export.js JSON) to Markdown files.",
                     "The export zip, its extracted folder, conversations.json, or a chatgpt_export.js JSON file")
    args = ap.parse_args()
    set_timezone(args.tz)

    src = Path(args.input).expanduser()
    export = Export(src)
    out_dir = Path(args.out).expanduser() if args.out else default_out(src)
    writer = Writer(out_dir, "ChatGPT conversations")
    render = Renderer(export, writer.out_dir, args)

    for wrapper, conv, err in load_conversations(export.conversations()):
        title = wrapper.get("title") or "Untitled"
        if conv is None or not conv.get("mapping"):
            writer.fail(title, wrapper.get("id", ""), err or "no conversation data")
            continue
        try:
            md, n, title, cid, created = convert(wrapper, conv, args, render)
        except Exception as e:  # keep going on one bad conversation
            writer.fail(title, wrapper.get("id", ""), f"conversion error: {e}")
            continue
        writer.add(md, title, cid, created, n)

    for chat in export.group_chats():
        try:
            md, n, title, cid, created = convert_group_chat(chat, args, render)
        except Exception as e:
            writer.fail(chat.get("name") or "Group chat", chat.get("id", ""), f"conversion error: {e}")
            continue
        writer.add(md, "Group chat: " + title, cid, created, n)
    writer.finish()


if __name__ == "__main__":
    main()
