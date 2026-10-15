#!/usr/bin/env python3
"""
Convert a claude.ai data export to Markdown, one file per conversation, in the same
layout as chatgpt_json_to_md.py.

claude.ai (Settings > Privacy > Export data) emails a manifest of zip files, which
claude_download_export.js downloads. Pass the folder that holds them, any of the zips,
or the extracted JSON files:
  conversations-000.zip > conversations.json
      [{uuid, name, summary, created_at, updated_at,
        chat_messages: [{uuid, parent_message_uuid, sender, created_at, text, files, attachments,
                         content: [{type: text | thinking | tool_use | tool_result, ...}]}]}]
  projects-000.zip      > projects/<uuid>.json   (instructions and knowledge files)
  memories-000.zip      > memories/<uuid>.json   (memory summary and memory files)
  light_metadata-000.zip > users.json            (names and emails, used for --users)
The other zips (feedback, frames) are not converted.

Team and Enterprise exports: when an owner exports the organization, the export holds
every member's conversations, projects and memory. Each conversation records the account
that owns it, so you can convert only some members:
  python3 claude_json_to_md.py team-export --list-users                 # who is in the export
  python3 claude_json_to_md.py team-export --users jane@example.edu     # one member
  python3 claude_json_to_md.py team-export --users 1a2b3c4d "John Roe"  # several members
--users takes account IDs (or their first 8 or more characters), email addresses or full
names, and part of a name or email when only one member matches. When more than one
member is converted, each gets a subfolder named after them, with its own _index.md,
projects/ and memory/, and the output folder's _index.md lists the members. Projects go
to the member who created them.

Usage:
  python3 claude_json_to_md.py ~/Downloads/claude-export           # folder with the zips
  python3 claude_json_to_md.py conversations-000.zip -o my_chats --include-tools --timestamps

Options:
  -o, --out DIR        Output folder (default: <input name>_md next to the input)
  --users WHO ...      Convert only these members (account ID, email or name; see above)
  --list-users         List the members in the export with their number of conversations,
                       projects and memory, then stop
  --include-tools      Also include tool calls and their output (web searches, code runs, file reads)
  --include-thinking   Include Claude's thinking as collapsible blocks (only the short summary
                       when the export does not have the full text)
  --timestamps         Add a timestamp under each message heading
  --tz ZONE            Time zone for dates, e.g. America/New_York (default: your computer's local zone)
  --all-branches       Append the other branches of a conversation (edited prompts and retried
                       replies). By default only the branch with the newest message is written,
                       because the export does not record which branch was on screen.
  --full-tool-output   Do not shorten long tool output (default: 3000 characters)
  --strip-citations    Leave out the web-search source links after cited sentences
  --include-summary    Start each file with Claude's summary of the conversation, when it has one

Output:
  <date> <title>.md    one file per conversation, and _index.md listing them newest first
  projects/<name>/     _project.md (description, instructions, project memory) and each
                       knowledge file as its own .md file
  memory/              memory.md (memory summary and project memories) and the memory files

Artifacts, drafts from the message composer, image search results and the names of files
Claude shared are always kept, because they were part of the reply on screen. The export
has the names of uploaded and shared files but not the files themselves.

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import html
import json
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from md_common import (Writer, attachments_line, base_parser, details, document, fence,
                       fmt_dt, message_block, safe_filename, set_timezone, short_title, sort_key)

TOOL_OUTPUT_LIMIT = 3000
SENDER_LABEL = {"human": "User", "assistant": "Claude"}
CHAT_URL = "https://claude.ai/chat/"
PROJECT_URL = "https://claude.ai/project/"


# ---------------------------------------------------------------- input loading
class Export:
    """Conversations, projects and memories found in folders, zip files or JSON files."""

    def __init__(self, inputs):
        self.conversations, self.projects, self.memories, self.unreadable = {}, {}, {}, []
        self.users = {}  # account uuid -> {full_name, email_address, ...} from users.json
        for i in inputs:
            p = Path(i).expanduser()
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    rel = f.relative_to(p).parts
                    if f.is_file() and f.suffix.lower() in (".zip", ".json") \
                            and not any(x.endswith("_md") for x in rel[:-1]):
                        self._file(f)
            elif p.is_file():
                self._file(p)
            else:
                self.unreadable.append((str(p), "not found"))

    def _file(self, f):
        try:
            if zipfile.is_zipfile(f):
                with zipfile.ZipFile(f) as z:
                    for n in z.namelist():
                        if n.lower().endswith(".json"):
                            self._json(z.read(n), f"{f.name}/{n}")
            elif f.suffix.lower() == ".json":
                self._json(f.read_bytes(), f.name)
        except (OSError, zipfile.BadZipFile) as e:
            self.unreadable.append((f.name, str(e)))

    def _json(self, raw, label):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as e:
            self.unreadable.append((label, f"not valid JSON: {e}"))
            return
        if isinstance(data, dict) and "chat_messages" in data:
            data = [data]  # a single conversation
        if isinstance(data, list) and data and all(
                isinstance(u, dict) and u.get("uuid") and "chat_messages" not in u
                and ("email_address" in u or "full_name" in u) for u in data):
            for u in data:  # users.json
                self.users[u["uuid"]] = u
        elif isinstance(data, list):
            for c in data:
                if isinstance(c, dict) and "chat_messages" in c:
                    key = c.get("uuid") or f"{label}#{len(self.conversations)}"
                    old = self.conversations.get(key)
                    # The same conversation can appear twice (zip and extracted copy): keep the fuller one.
                    if old is None or len(c.get("chat_messages") or []) > len(old.get("chat_messages") or []):
                        self.conversations[key] = c
        elif isinstance(data, dict) and "prompt_template" in data and "docs" in data:
            self.projects[data.get("uuid") or label] = data
        elif isinstance(data, dict) and ("conversations_memory" in data or "memory_files" in data):
            self.memories[data.get("account_uuid") or label] = data


# ---------------------------------------------------------------- members (Team and Enterprise exports)
def conv_owner(c):
    return (c.get("account") or {}).get("uuid")


def has_messages(c):
    """False for conversations whose messages are all empty in the export (no text, and at
    most uploads without a file name), which the converter lists under "Not converted"."""
    def named(items):
        return any(isinstance(f, dict) and (f.get("file_name") or f.get("extracted_content")) for f in items or [])
    return any(isinstance(m, dict) and (m.get("content") or (m.get("text") or "").strip()
                                        or named(m.get("files")) or named(m.get("attachments")))
               for m in c.get("chat_messages") or [])


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def project_owner(p):
    return (p.get("creator") or {}).get("uuid")


class Members:
    """Who owns what in the export, and helpers to name and select members."""

    def __init__(self, export):
        self.export = export
        self.convs, self.projects, self.memories = {}, {}, {}
        for c in export.conversations.values():
            self.convs.setdefault(conv_owner(c), []).append(c)
        for p in export.projects.values():
            self.projects.setdefault(project_owner(p), []).append(p)
        for key, m in export.memories.items():
            self.memories.setdefault(m.get("account_uuid"), {})[key] = m
        self.ids = (set(export.users) | set(self.convs) | set(self.projects) | set(self.memories)) - {None}

    def name(self, uid):
        u = self.export.users.get(uid) or {}
        creator = next(((p.get("creator") or {}).get("full_name") for p in self.projects.get(uid, [])), None)
        return u.get("full_name") or creator or u.get("email_address") or (uid[:8] if uid else "Unknown user")

    def email(self, uid):
        return (self.export.users.get(uid) or {}).get("email_address") or ""

    def owners(self):
        """Every owner with something to convert (None means no account recorded)."""
        return set(self.convs) | set(self.projects) | set(self.memories)

    def select(self, selectors):
        """Resolve --users values to account ids. Exits with a message when one does not match."""
        chosen = set()
        for sel in (s.strip() for raw in selectors for s in raw.split(",")):
            if not sel:
                continue
            low = sel.lower()
            tests = [
                lambda u: u.lower() == low,
                lambda u: len(low) >= 8 and u.lower().startswith(low),
                lambda u: self.email(u).lower() == low,
                lambda u: self.name(u).lower() == low,
                lambda u: low in self.name(u).lower() or low in self.email(u).lower(),
            ]
            for test in tests:
                hits = {u for u in self.ids if test(u)}
                if hits:
                    break
            if not hits:
                raise SystemExit(f'No member matches "{sel}". Run with --list-users to see who is in the export.')
            if len(hits) > 1:
                who = "\n".join(f"  {self.name(u)} <{self.email(u)}> {u}" for u in sorted(hits, key=self.name))
                raise SystemExit(f'"{sel}" matches more than one member. Use an email or account ID:\n{who}')
            chosen |= hits
        return chosen

    def table(self):
        rows = []
        for uid in sorted(self.ids | ({None} if None in self.owners() else set()),
                          key=lambda u: (-len(self.convs.get(u, [])), self.name(u).lower())):
            convs = self.convs.get(uid, [])
            full = sum(1 for c in convs if has_messages(c))
            rows.append((self.name(uid), self.email(uid), uid or "(no account)", str(full),
                         str(len(convs) - full), str(len(self.projects.get(uid, []))),
                         "yes" if self.memories.get(uid) else ""))
        head = ("Name", "Email", "Account ID", "Conversations", "Empty", "Projects", "Memory")
        widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
        line = lambda r: "  ".join(c.ljust(w) if i < 3 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))).rstrip()
        return "\n".join([line(head), line(tuple("-" * w for w in widths))] + [line(r) for r in rows])


# ---------------------------------------------------------------- message ordering
def threads(msgs):
    """Return (main thread, other branches).

    The main thread ends at the newest message. Each other branch is (fork message or
    None, messages after the fork), for the --all-branches option.
    """
    if not msgs or not all(m.get("uuid") and "parent_message_uuid" in m for m in msgs):
        return msgs, []  # no tree information: keep the export's order
    by_id = {m["uuid"]: m for m in msgs}
    has_child = {m.get("parent_message_uuid") for m in msgs}
    order = {m["uuid"]: i for i, m in enumerate(msgs)}
    leaves = sorted((m for m in msgs if m["uuid"] not in has_child),
                    key=lambda m: (sort_key(m.get("created_at")), order[m["uuid"]]))

    def chain(leaf, stop):
        out, seen, m = [], set(), leaf
        while m is not None and m["uuid"] not in stop and m["uuid"] not in seen:
            seen.add(m["uuid"])
            out.append(m)
            m = by_id.get(m.get("parent_message_uuid"))
        return list(reversed(out)), m

    main, _ = chain(leaves[-1], set())
    done = {m["uuid"] for m in main}
    branches = []
    for leaf in reversed(leaves[:-1]):
        seg, fork = chain(leaf, done)
        done.update(m["uuid"] for m in seg)
        branches.append((fork, seg))
    return main, branches


# ---------------------------------------------------------------- small helpers
def link(title, url):
    title = (title or url).replace("[", "\\[").replace("]", "\\]")
    if any(c in url for c in " ()<>"):
        url = "<" + url.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E") + ">"
    return f"[{title}]({url})"


def domain(url):
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host or url


def pretty(value):
    """Pretty-print JSON (given as a string or object) and return (text, fence language)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False), "json"
    if not isinstance(value, str):
        return str(value), ""
    s = value.strip()
    if s[:1] in "{[":
        try:
            return json.dumps(json.loads(s), indent=2, ensure_ascii=False), "json"
        except ValueError:
            pass
    return value, ""


LANG_BY_SUFFIX = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
                  ".jsx": "jsx", ".tsx": "tsx", ".sh": "bash", ".md": "markdown", ".html": "html",
                  ".css": "css", ".json": "json", ".tex": "latex", ".yaml": "yaml", ".yml": "yaml",
                  ".r": "r", ".sql": "sql", ".svg": "xml", ".xml": "xml", ".c": "c", ".cpp": "cpp"}

ARTIFACT_LANG = {"text/markdown": "markdown", "text/html": "html", "image/svg+xml": "svg",
                 "application/vnd.ant.mermaid": "mermaid", "application/vnd.ant.react": "jsx"}


class Ctx:
    def __init__(self, args, titles):
        self.args = args
        self.titles = titles  # url -> page title, from the conversation's search results

    def shorten(self, text):
        if self.args.full_tool_output or len(text) <= TOOL_OUTPUT_LIMIT:
            return text
        return text[:TOOL_OUTPUT_LIMIT] + f"\n... [shortened, {len(text):,} characters in total]"


def page_titles(conv):
    titles = {}
    for m in conv.get("chat_messages") or []:
        for p in m.get("content") or []:
            if p.get("type") == "tool_result" and isinstance(p.get("content"), list):
                for it in p["content"]:
                    if isinstance(it, dict) and it.get("url") and it.get("title"):
                        titles.setdefault(it["url"], it["title"])
    return titles


# ---------------------------------------------------------------- text and citations
def apply_citations(text, citations, ctx):
    """Add '(Source; Source)' links after each cited passage, like the ChatGPT converter."""
    if ctx.args.strip_citations or not citations:
        return text
    groups = {}
    for c in citations:
        if not isinstance(c, dict):
            continue
        url = (c.get("details") or {}).get("url") or c.get("url")
        end = c.get("end_index")
        if url and isinstance(end, int) and 0 < end <= len(text):
            urls = groups.setdefault(end, [])
            if url not in urls:
                urls.append(url)
    for end in sorted(groups, reverse=True):
        links = "; ".join(link(ctx.titles.get(u) or domain(u), u) for u in groups[end])
        sep = "" if text[end - 1].isspace() else " "
        text = f"{text[:end]}{sep}({links}){text[end:]}"
    return text


def render_thinking(p, ctx):
    if not ctx.args.include_thinking:
        return ""
    text = (p.get("thinking") or "").strip()
    if text:
        return details("Thinking", text)
    sums = [s.get("summary") for s in p.get("summaries") or [] if isinstance(s, dict) and s.get("summary")]
    return details("Thinking (summary only)", "\n".join(f"- {s}" for s in sums)) if sums else ""


# ---------------------------------------------------------------- tools
def visible_output(name, inp, result):
    """What a tool call showed in the chat itself, kept even without --include-tools."""
    base = name.split(":")[-1]
    if base == "artifacts":
        title = inp.get("title") or inp.get("id") or "Artifact"
        if inp.get("command") in ("create", "rewrite") and inp.get("content"):
            t = inp.get("type") or ""
            lang = inp.get("language") if t == "application/vnd.ant.code" else ARTIFACT_LANG.get(t, "")
            return f"**Artifact: {title}**\n\n{fence(inp['content'], lang or '')}"
        return f"*[Artifact updated: {title}]*"
    if base.startswith("message_compose"):
        kind = inp.get("kind") or "message"
        out = []
        for v in inp.get("variants") or []:
            if not isinstance(v, dict):
                continue
            head = f"**Draft {kind}: {v.get('label') or ''}**".replace(": **", "**")
            subj = f"\n\n*Subject:* {v['subject']}" if v.get("subject") else ""
            body = "\n".join("> " + line if line else ">" for line in (v.get("body") or "").split("\n"))
            out.append(f"{head}{subj}\n\n{body}")
        return "\n\n".join(out)
    if base == "show_widget":
        return f"*[Interactive widget: {inp.get('title') or 'widget'}]*"
    if base == "present_files":
        paths = inp.get("filepaths") or []
        names = [PurePosixPath(p).name for p in paths if isinstance(p, str)]
        return ("*[Files shared: " + ", ".join(f"`{n}`" for n in names) + "]*") if names else ""
    if base == "image_search" and result:
        imgs = [im for it in result.get("content") or [] if isinstance(it, dict)
                for im in it.get("images") or [] if isinstance(im, dict)]
        rows = [f"- {link(im.get('title'), im.get('page_url') or im.get('url'))}"
                for im in imgs if im.get("page_url") or im.get("url")]
        return ("*Images shown:*\n\n" + "\n".join(rows)) if rows else ""
    return ""


# Input keys that visible_output already shows, so --include-tools does not repeat them.
SHOWN_KEYS = {"artifacts": {"content"}, "message_compose_v1": {"variants"}}


def tool_input(name, inp, ctx):
    if not isinstance(inp, dict):
        text, lang = pretty(inp)
        return fence(ctx.shorten(text), lang)
    skip = SHOWN_KEYS.get(name.split(":")[-1], set())
    lines, blocks = [], []
    for k, v in inp.items():
        if k in skip or v in (None, "", [], {}):
            continue
        if isinstance(v, str) and (k in ("command", "code", "widget_code") or "\n" in v or len(v) > 200):
            lang = {"command": "bash", "widget_code": "html"}.get(k) or inp.get("language") \
                or LANG_BY_SUFFIX.get(PurePosixPath(str(inp.get("path") or "")).suffix.lower(), "")
            blocks.append(f"`{k}`\n\n{fence(ctx.shorten(v), lang)}")
        elif isinstance(v, (dict, list)):
            text = json.dumps(v, indent=2, ensure_ascii=False)
            if len(text) < 120 and "\n" not in json.dumps(v, ensure_ascii=False):
                lines.append(f"- {k}: `{json.dumps(v, ensure_ascii=False)}`")
            else:
                blocks.append(f"`{k}`\n\n{fence(ctx.shorten(text), 'json')}")
        else:
            lines.append(f"- {k}: {v}")
    return "\n\n".join(x for x in ("\n".join(lines), "\n\n".join(blocks)) if x)


def tool_output(result, ctx):
    items = result.get("content")
    if isinstance(items, str):
        items = [{"type": "text", "text": items}]
    out, links = [], []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t == "knowledge":  # web search and fetch results: list the pages, not their text
            links.append(f"- {link(it.get('title'), it['url'])}" if it.get("url") else f"- {it.get('title')}")
        elif t == "text":
            text, lang = pretty(it.get("text") or "")
            if text.strip():
                out.append(fence(ctx.shorten(text), lang))
        elif t == "local_resource":
            out.append(f"- `{it.get('file_path') or it.get('name')}`")
        elif t == "image_gallery":
            links += [f"- {link(im.get('title'), im.get('page_url') or im.get('url'))}"
                      for im in it.get("images") or [] if isinstance(im, dict) and (im.get("page_url") or im.get("url"))]
        elif t == "image":
            out.append("*[Image]*")
        else:
            text, lang = pretty(it)
            out.append(fence(ctx.shorten(text), lang))
    if links:
        out.insert(0, "\n".join(links))
    return "\n\n".join(out)


def render_tool(use, result, ctx):
    name = (use or result or {}).get("name") or "tool"
    inp = (use or {}).get("input") or {}
    visible = visible_output(name, inp if isinstance(inp, dict) else {}, result)
    if not ctx.args.include_tools:
        return visible
    body = []
    if use is not None:
        i = tool_input(name, inp, ctx)
        if i:
            body.append(f"**Input**\n\n{i}")
    if result is not None:
        o = tool_output(result, ctx)
        if o:
            body.append(f"**{'Error' if result.get('is_error') else 'Output'}**\n\n{o}")
    note = (use or {}).get("message") or ""
    summary = f"Tool call: {name}" + (f" ({note})" if note and note not in (name, name.split(':')[-1]) else "")
    block = details(html.escape(summary, quote=False), "\n\n".join(body)) if body \
        else f"*[{html.escape(summary, quote=False)}]*"
    return "\n\n".join(x for x in (block, visible) if x)


# ---------------------------------------------------------------- messages
def render_part(p, ctx):
    t = p.get("type")
    if t == "text":
        return apply_citations(p.get("text") or "", p.get("citations"), ctx).strip()
    if t == "thinking":
        return render_thinking(p, ctx)
    if t in ("token_budget", "injected_prompt_block"):
        return ""  # bookkeeping and system text the chat does not show
    if t == "image":
        return "*[Image]*"
    for k in ("text", "content"):  # unknown type: keep any text it has
        if isinstance(p.get(k), str) and p[k].strip():
            return p[k].strip()
    return ""


def render_content(m, ctx):
    parts = [p for p in m.get("content") or [] if isinstance(p, dict)]
    if not parts:
        return (m.get("text") or "").strip()
    out, pending = [], None
    for p in parts:
        t = p.get("type")
        if t == "tool_use":
            if pending is not None:
                out.append(render_tool(pending, None, ctx))
            pending = p
            continue
        if t == "tool_result":
            rid = p.get("tool_use_id")
            if pending is not None and (not rid or not pending.get("id") or rid == pending.get("id")):
                out.append(render_tool(pending, p, ctx))
            else:
                if pending is not None:
                    out.append(render_tool(pending, None, ctx))
                out.append(render_tool(None, p, ctx))
            pending = None
            continue
        if pending is not None:
            out.append(render_tool(pending, None, ctx))
            pending = None
        out.append(render_part(p, ctx))
    if pending is not None:
        out.append(render_tool(pending, None, ctx))
    return "\n\n".join(x for x in out if x and x.strip())


def message_files(m):
    """(attachments line, collapsible blocks with the text of pasted or uploaded documents)."""
    files = [f for f in m.get("files") or [] if isinstance(f, dict)]
    atts = [a for a in m.get("attachments") or [] if isinstance(a, dict)]
    line = attachments_line([f.get("file_name") for f in files] + [a.get("file_name") for a in atts])
    blocks = [details(f"Attachment text: {html.escape(a.get('file_name') or 'pasted text', quote=False)}",
                      fence(a["extracted_content"]))
              for a in atts if (a.get("extracted_content") or "").strip()]
    return line, "\n\n".join(blocks)


def render_thread(msgs, ctx, level="##"):
    """Message blocks for one thread, merging consecutive assistant messages like the other converters."""
    out, n, last = [], 0, None
    for m in msgs:
        att, pasted = message_files(m)
        body = "\n\n".join(x for x in (pasted, render_content(m, ctx)) if x)
        if not body and not att:
            continue
        role = m.get("sender") or "unknown"
        if role == last == "assistant" and not ctx.args.timestamps and not att:
            out.append(body)
        else:
            block = message_block(SENDER_LABEL.get(role, role.title()), body, m.get("created_at"),
                                  att, ctx.args.timestamps)
            out.append(block if level == "##" else level + block[2:])
        last = role
        n += 1
    return out, n


# ---------------------------------------------------------------- conversation -> markdown
def message_text(m):
    """Plain text of a message, without citations, thinking or tools."""
    texts = [p.get("text") or "" for p in m.get("content") or []
             if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(t for t in texts if t.strip()) or (m.get("text") or "")


def first_prompt(msgs):
    for m in msgs:
        if m.get("sender") == "human" and message_text(m).strip():
            return message_text(m)
    return ""


def convert(conv, args, user=""):
    msgs = [m for m in conv.get("chat_messages") or [] if isinstance(m, dict)]
    title = (conv.get("name") or "").strip() or short_title(first_prompt(msgs))
    cid = conv.get("uuid") or ""
    ctx = Ctx(args, page_titles(conv))

    main, branches = threads(msgs)
    out, n_msgs = render_thread(main, ctx)

    if args.all_branches and branches:
        extra, k = [], 0
        for fork, seg in branches:
            blocks, n = render_thread(seg, ctx, level="###")
            if not n:
                continue
            k += 1
            if fork is None:
                where = "at the start"
            else:
                quote = short_title(message_text(fork), 60)
                who = SENDER_LABEL.get(fork.get("sender"), "a")
                where = f"after {who} message “{quote}”"
            extra.append(f"## Branch {k} (splits off {where})")
            extra += blocks
        if extra:
            out += ["---\n\n# Other branches"] + extra

    if args.include_summary and (conv.get("summary") or "").strip():
        out.insert(0, details("Conversation summary", conv["summary"].strip()))

    created = conv.get("created_at") or (msgs[0].get("created_at") if msgs else None)
    front = [
        ("title", title),
        ("id", cid),
        ("url", CHAT_URL + cid if cid else ""),
        ("created", fmt_dt(created)),
        ("updated", fmt_dt(conv.get("updated_at"))),
        ("user", user),
    ]
    return document(title, front, out), n_msgs, title, cid, created


# ---------------------------------------------------------------- projects and memory
def unique_dir(parent, name, used):
    base, k = name, 2
    while name.lower() in used:
        name, k = f"{base} ({k})", k + 1
    used.add(name.lower())
    return parent / name


def doc_filename(name):
    fn = safe_filename(name or "document", 120)
    return fn if fn.lower().endswith((".md", ".txt")) else fn + ".md"


def md_link(text, rel):
    return f"[{text}]({rel.replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"


def write_projects(projects, memories, out_dir):
    """Write projects/<name>/_project.md and the knowledge files. Returns index lines."""
    proj_mem = {}
    for mem in memories.values():
        proj_mem.update(mem.get("project_memories") or {})
    used, index = set(), []
    for p in sorted(projects.values(), key=lambda p: p.get("name") or ""):
        name = p.get("name") or (p.get("uuid") or "project")[:8]
        folder = unique_dir(out_dir / "projects", safe_filename(name), used)
        folder.mkdir(parents=True, exist_ok=True)
        blocks, names = [], set()
        if (p.get("description") or "").strip():
            blocks.append(p["description"].strip())
        if (p.get("prompt_template") or "").strip():
            blocks.append("## Instructions\n\n" + p["prompt_template"].strip())
        if (proj_mem.get(p.get("uuid")) or "").strip():
            blocks.append("## Project memory\n\n" + proj_mem[p["uuid"]].strip())
        docs = []
        for d in p.get("docs") or []:
            fn = doc_filename(d.get("filename"))
            while fn.lower() in names:
                fn = "copy of " + fn
            names.add(fn.lower())
            (folder / fn).write_text(d.get("content") or "", encoding="utf-8")
            docs.append(f"- {md_link(d.get('filename') or fn, fn)}")
        if docs:
            blocks.append(f"## Knowledge files ({len(docs)})\n\n" + "\n".join(docs))
        visibility = {True: "private", False: "shared"}.get(p.get("is_private"), "")
        front = [("title", name), ("id", p.get("uuid") or ""),
                 ("url", PROJECT_URL + p["uuid"] if p.get("uuid") else ""),
                 ("created", fmt_dt(p.get("created_at"))), ("updated", fmt_dt(p.get("updated_at"))),
                 ("creator", (p.get("creator") or {}).get("full_name") or ""), ("visibility", visibility)]
        (folder / "_project.md").write_text(document(name, front, blocks), encoding="utf-8")
        index.append(f"- {md_link(name, f'projects/{folder.name}/_project.md')} ({len(docs)} knowledge files)")
    return index


def write_memory(memories, projects, out_dir):
    """Write memory/memory.md and the memory files. Returns index lines."""
    names = {p.get("uuid"): p.get("name") for p in projects.values()}
    index = []
    for k, mem in enumerate(memories.values()):
        folder = out_dir / "memory" / (f"account-{k + 1}" if len(memories) > 1 else "")
        folder.mkdir(parents=True, exist_ok=True)
        blocks = []
        if (mem.get("conversations_memory") or "").strip():
            blocks.append("## Memory summary\n\n" + mem["conversations_memory"].strip())
        for pid, text in (mem.get("project_memories") or {}).items():
            if (text or "").strip():
                blocks.append(f"## Project memory: {names.get(pid) or pid}\n\n{text.strip()}")
        files = []
        for f in mem.get("memory_files") or []:
            if not isinstance(f, dict) or not f.get("path"):
                continue
            parts = [safe_filename(x, 120) for x in PurePosixPath(f["path"]).parts if x not in ("/", ".", "..")]
            if not parts:
                continue
            target = folder.joinpath("files", *parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.get("content") or "", encoding="utf-8")
            when = fmt_dt(f.get("updated_at"))
            files.append(f"- {md_link(f['path'], 'files/' + '/'.join(parts))}" + (f" (updated {when})" if when else ""))
        if files:
            blocks.append(f"## Memory files ({len(files)})\n\n" + "\n".join(files))
        (folder / "memory.md").write_text(document("Claude memory", [("title", "Claude memory")], blocks),
                                          encoding="utf-8")
        rel = (folder / "memory.md").relative_to(out_dir).as_posix()
        index.append(f"- {md_link('Claude memory', rel)} ({len(files)} memory files)")
    return index


# ---------------------------------------------------------------- main
def main():
    ap = base_parser("Convert a claude.ai data export to Markdown files.",
                     "Export folder, zip files, or extracted JSON files", multiple_inputs=True)
    ap.add_argument("--all-branches", action="store_true", help="Append the other branches of each conversation")
    ap.add_argument("--full-tool-output", action="store_true", help="Do not shorten long tool output")
    ap.add_argument("--strip-citations", action="store_true", help="Leave out web-search source links")
    ap.add_argument("--include-summary", action="store_true", help="Start each file with Claude's conversation summary")
    ap.add_argument("--users", nargs="+", metavar="WHO",
                    help="Convert only these members: account IDs (or 8+ leading characters), emails or names")
    ap.add_argument("--list-users", action="store_true",
                    help="List the members in the export and what each has, then stop")
    args = ap.parse_args()
    set_timezone(args.tz)

    first = Path(args.inputs[0]).expanduser().resolve()
    default = first.with_name(first.name + "_md") if first.is_dir() else first.with_name(first.stem + "_md")
    export = Export(args.inputs)
    if not (export.conversations or export.projects or export.memories):
        raise SystemExit("No claude.ai export data (conversations, projects or memories) found in the input.")
    members = Members(export)

    if args.list_users:
        print(members.table())
        return

    owners = members.owners()
    if args.users:
        chosen = members.select(args.users)
        for uid in sorted(chosen - owners, key=members.name):
            print(f"Note: {members.name(uid)} has no conversations, projects or memory in this export.")
        owners &= chosen
        if not owners:
            raise SystemExit("Nothing to convert for the selected members.")

    out_dir = Path(args.out).expanduser() if args.out else default
    for name, err in export.unreadable:
        print(f"Could not read {name}: {err}")
    if len(owners) == 1:
        uid = next(iter(owners))
        convert_member(members, uid, out_dir, "Claude conversations", args, export.unreadable)
        return

    # Several members: one subfolder each, and an index of members at the top.
    out_dir.mkdir(parents=True, exist_ok=True)
    used, rows = set(), []
    for uid in sorted(owners, key=lambda u: (u is None, members.name(u).lower())):
        name = members.name(uid)
        folder = unique_dir(out_dir, safe_filename(name), used)
        n_conv, n_proj = convert_member(members, uid, folder, f"Claude conversations of {name}", args, [])
        email = f" <{members.email(uid)}>" if members.email(uid) else ""
        rows.append(f"- {md_link(name, folder.name + '/_index.md')}{email}: "
                    f"{plural(n_conv, 'conversation')}, {plural(n_proj, 'project')}"
                    + (", memory" if members.memories.get(uid) else ""))
    lines = [f"# Claude export by member ({len(rows)})", ""] + rows
    if export.unreadable:
        lines += ["", f"## Not read ({len(export.unreadable)})", ""] + [f"- {n}: {e}" for n, e in export.unreadable]
    (out_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} members to {out_dir} (listed in _index.md)")


def convert_member(members, uid, out_dir, heading, args, unreadable):
    """Convert one member's conversations, projects and memory into out_dir.

    Returns (conversations written, projects written).
    """
    user = members.name(uid) if uid else ""
    writer = Writer(out_dir, heading)
    for name, err in unreadable:
        writer.fail(name, "", f"could not read: {err}")
    for conv in members.convs.get(uid, []):
        cid = conv.get("uuid") or ""
        title = conv.get("name") or cid
        try:
            md, n, title, cid, created = convert(conv, args, user)
        except Exception as e:  # keep going on one bad conversation
            writer.fail(title, cid, f"conversion error: {e}")
            continue
        if not n:
            writer.fail(title, cid, "conversation has no messages")
            continue
        writer.add(md, title, cid, created, n)
    writer.finish()

    projects = {p.get("uuid") or str(i): p for i, p in enumerate(members.projects.get(uid, []))}
    memories = members.memories.get(uid, {})
    extra = []
    if projects:
        lines = write_projects(projects, memories, writer.out_dir)
        extra += ["", f"## Projects ({len(lines)})", ""] + lines
        print(f"Wrote {len(lines)} projects to {writer.out_dir / 'projects'}")
    if memories:
        lines = write_memory(memories, members.export.projects, writer.out_dir)
        extra += ["", "## Memory", ""] + lines
        print(f"Wrote memory to {writer.out_dir / 'memory'}")
    if extra:
        with open(writer.out_dir / "_index.md", "a", encoding="utf-8") as f:
            f.write("\n".join(extra) + "\n")
    return len(writer.index), len(projects)


if __name__ == "__main__":
    main()
