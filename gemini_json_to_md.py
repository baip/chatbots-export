#!/usr/bin/env python3
"""
Convert a Google Takeout export of Gemini Apps activity to Markdown, one file per
conversation, in the same layout as chatgpt_json_to_md.py.

Takeout (takeout.google.com > My Activity > Gemini Apps, JSON format) gives you a zip
(takeout-<account>-<date>-1-001.zip) holding "Takeout/My Activity/Gemini Apps/MyActivity.json"
next to the files you attached and the images Gemini generated. Pass the zip itself, the
extracted Takeout folder, or the MyActivity.json file. The activity log has one entry per prompt rather than per conversation,
and it has no conversation titles. This script:
  * groups the entries into conversations by their gemini.google.com/app/<id> link;
  * converts each response from HTML to Markdown;
  * puts each canvas into the conversation of the prompt just before it (document
    canvases inline, app/code canvases saved to a canvas/ subfolder and linked);
  * links attached and generated files: to their copies in the Takeout folder, or, when
    the input is a zip, to copies extracted into a files/ subfolder of the output;
  * names each conversation after its first prompt, or after a title found with --titles.

Usage:
  python3 gemini_json_to_md.py takeout-xudd09-20261015T034159Z-1-001.zip
  python3 gemini_json_to_md.py "Takeout/My Activity/Gemini Apps/MyActivity.json"
  python3 gemini_json_to_md.py MyActivity.json --titles ~/Chatbots/Gemini --timestamps

Options:
  -o, --out DIR     Output folder (default: <zip name>_md next to the zip, or MyActivity_md
                    next to MyActivity.json)
  --titles DIR      Folder of Markdown files saved from gemini.google.com that contain a
                    "Source: https://gemini.google.com/app/<id>" line (for example from a
                    browser extension); their "# Title" headings become the conversation
                    titles. Can be given more than once.
  --timestamps      Add a timestamp under each message heading
  --tz ZONE         Time zone for dates, e.g. America/New_York (default: your computer's local zone)

When Takeout splits a large export into several zips (-1-001.zip, -1-002.zip, ...), pass
any one of them; the other parts in the same folder are read too.

Entries that hold no conversation content ("Used Gemini Apps", feedback, survey answers,
"Selected preferred draft") are skipped. Takeout does not record which model answered.

Only the Python standard library is needed (Python 3.8+). Shared helpers are in md_common.py.
"""
import json
import os
import re
import shutil
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from md_common import (Writer, base_parser, details, document, fence, fmt_dt, message_block,
                       safe_filename, set_timezone, short_title, sort_key, to_dt)

CANVAS_WINDOW = 2 * 3600  # a canvas belongs to the last prompt at most this many seconds earlier
PLACEHOLDER = re.compile(r"^https?://googleusercontent\.com/\w+/\d+\s*$")


# ================================================================ HTML -> Markdown
VOID = {"br", "hr", "img", "input", "meta", "link", "col", "wbr", "source"}
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "blockquote",
         "table", "thead", "tbody", "tfoot", "tr", "th", "td", "hr", "section", "article",
         "figure", "figcaption", "dl", "dt", "dd", "details", "summary"}


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text_of(n):
    return n if isinstance(n, str) else "".join(text_of(c) for c in n.children)


def _wrap(mark, s):
    core = s.strip()
    if not core:
        return s
    lead, trail = s[: len(s) - len(s.lstrip())], s[len(s.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _code_span(s):
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{ticks}{pad}{s}{pad}{ticks}"


def _inline(nodes):
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(re.sub(r"\s+", " ", n))
            continue
        t = n.tag
        if t == "br":
            out.append("  \n")
        elif t in ("strong", "b"):
            out.append(_wrap("**", _inline(n.children)))
        elif t in ("em", "i"):
            out.append(_wrap("*", _inline(n.children)))
        elif t in ("del", "s", "strike"):
            out.append(_wrap("~~", _inline(n.children)))
        elif t == "code":
            out.append(_code_span(text_of(n)))
        elif t == "a":
            label, href = _inline(n.children).strip(), (n.attrs.get("href") or "").strip()
            if not href:
                out.append(label)
            elif not label or label == href:
                out.append(f"<{href}>")
            else:
                out.append(f"[{label}]({href})")
        elif t == "img":
            src = n.attrs.get("src") or ""
            out.append(f"![{n.attrs.get('alt') or ''}]({src})" if src else "")
        elif t in ("sup", "sub"):
            out.append(f"<{t}>{_inline(n.children).strip()}</{t}>")
        else:
            out.append(_inline(n.children))
    return "".join(out)


def _tidy(s):
    s = re.sub(r"  \n[ \t]+", "  \n", s)
    return s.strip(" \t\n")


def _blocks(nodes):
    """Render a list of nodes as Markdown blocks (list of strings)."""
    out, run = [], []

    def flush():
        txt = _tidy(_inline(run))
        if txt:
            out.append(txt)
        run.clear()

    for n in nodes:
        if isinstance(n, str) or n.tag not in BLOCK:
            run.append(n)
            continue
        flush()
        b = _block(n)
        if b:
            out.append(b)
    flush()
    return out


def _json_paragraph(n):
    """Gemini stores widgets and Gmail drafts as a paragraph of raw JSON; fold it away."""
    raw = text_of(n).strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("component") == "LlmGeneratedComponent":
        label = "Interactive widget specification"
    elif "contentMetadata" in obj or "emailDraft" in raw[:200]:
        label = "Email draft data"
    else:
        label = "Data"
    return details(label, fence(json.dumps(obj, indent=2, ensure_ascii=False), "json"))


def _block(n):
    t = n.tag
    if t == "p":
        j = _json_paragraph(n)
        if j:
            return j
    if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
        txt = _tidy(_inline(n.children)).replace("  \n", " ")
        return "#" * int(t[1]) + " " + txt if txt else ""
    if t == "hr":
        return "---"
    if t == "pre":
        j = _json_paragraph(n)
        if j and "LlmGeneratedComponent" in j:  # widget spec, not code the user asked for
            return j
        lang = ""
        for c in n.children:
            if isinstance(c, Node) and c.tag == "code":
                m = re.search(r"(?:language|lang)-([\w+#.-]+)", c.attrs.get("class") or "")
                lang = m.group(1) if m else ""
        return fence(text_of(n).rstrip("\n"), lang)
    if t == "blockquote":
        inner = "\n\n".join(_blocks(n.children))
        return "\n".join("> " + ln if ln else ">" for ln in inner.split("\n"))
    if t in ("ul", "ol"):
        return _list(n)
    if t == "li":  # stray list item
        return _list(Node("ul", []), [n])
    if t == "table":
        return _table(n)
    return "\n\n".join(_blocks(n.children))


def _list(n, items=None):
    ordered = n.tag == "ol"
    try:
        num = int(n.attrs.get("start") or 1)
    except ValueError:
        num = 1
    items = items if items is not None else [
        c for c in n.children if isinstance(c, Node) and c.tag == "li"]
    rendered, any_loose = [], False
    for li in items:
        marker = f"{num}. " if ordered else "- "
        num += 1
        loose = any(isinstance(c, Node) and c.tag == "p" for c in li.children)
        any_loose = any_loose or loose
        body = ("\n\n" if loose else "\n").join(_blocks(li.children))
        pad = " " * len(marker)
        lines = body.split("\n")
        rendered.append(marker + lines[0] + "".join("\n" + (pad + ln if ln else "") for ln in lines[1:]))
    return ("\n\n" if any_loose else "\n").join(rendered)


def _table(n):
    rows = []

    def collect(node):
        for c in node.children:
            if not isinstance(c, Node):
                continue
            if c.tag == "tr":
                rows.append([x for x in c.children if isinstance(x, Node) and x.tag in ("th", "td")])
            elif c.tag in ("thead", "tbody", "tfoot"):
                collect(c)

    collect(n)
    if not rows:
        return ""

    def cell(c):
        s = "<br>".join(_blocks(c.children)).replace("  \n", "<br>").replace("\n", "<br>")
        return s.replace("|", "\\|").strip()

    width = max(len(r) for r in rows)
    aligns = []
    for c in rows[0] + [None] * (width - len(rows[0])):
        a = (c.attrs.get("align") or "").lower() if c is not None else ""
        aligns.append({"left": ":---", "center": ":---:", "right": "---:"}.get(a, "---"))
    lines = []
    for i, r in enumerate(rows):
        cells = [cell(c) for c in r] + [""] * (width - len(r))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join(aligns) + " |")
    return "\n".join(lines)


def html_to_md(html):
    p = _Tree()
    p.feed(html or "")
    p.close()
    md = "\n\n".join(b for b in _blocks(p.root.children) if b.strip())
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# ================================================================ titles from saved pages
def load_titles(folders):
    """Map conversation id -> title from Markdown files that carry a 'Source:' link."""
    titles = {}
    for folder in folders or []:
        for f in sorted(Path(folder).expanduser().glob("*.md")):
            try:
                with f.open(encoding="utf-8") as fh:
                    head = [next(fh, "") for _ in range(15)]
            except OSError:
                continue
            title = next((ln[2:].strip() for ln in head if ln.startswith("# ")), "")
            m = next((re.search(r"gemini\.google\.com/(?:u/\d+/)?app/([\w-]+)", ln)
                      for ln in head if "gemini.google.com" in ln), None)
            if title and m:
                titles[m.group(1)] = re.sub(r"\s*-\s*Google Gemini$", "", title).strip()
    return titles


# ================================================================ activity -> conversations
def conv_id(url):
    m = re.search(r"/app/([\w-]+)", url or "")
    return m.group(1) if m else (url or "")


def strip_prefix(title, prefix):
    return title[len(prefix):] if title.startswith(prefix) else None


class Turn:
    def __init__(self, item):
        self.time = item.get("time")
        self.prompt, self.note = None, ""
        title = item.get("title") or ""
        if strip_prefix(title, "Prompted ") is not None:
            self.prompt = strip_prefix(title, "Prompted ")
        elif strip_prefix(title, "Added chat from link: ") is not None:
            self.prompt = strip_prefix(title, "Added chat from link: ")
            self.note = "Added from a shared chat link"
        else:
            self.note = title
        self.responses = [h.get("html") or "" for h in item.get("safeHtmlItem") or [] if isinstance(h, dict)]
        self.user_files, self.context_links, self.extra = [], [], []
        for s in item.get("subtitles") or []:
            name, url = (s.get("name") or "").strip(), (s.get("url") or "").strip()
            if url and "://" not in url:
                self.user_files.append((re.sub(r"^-\s*", "", name) or url, url))
            elif url.startswith("http") or name.startswith("http"):
                if not PLACEHOLDER.match(url or name):
                    self.context_links.append(url or name)
            elif re.match(r"^Attached \d+ files?\.$", name):
                continue
            elif name.startswith("[Image of ") or re.match(r"^\d+ generated images?\.$", name):
                self.extra.append(name)
        user_paths = {u for _, u in self.user_files}
        if item.get("imageFile") and item["imageFile"] not in user_paths:
            self.user_files.append((item["imageFile"], item["imageFile"]))
            user_paths.add(item["imageFile"])
        self.generated = [f for f in item.get("attachedFiles") or [] if f not in user_paths]
        self.canvases = []

    def has_content(self):
        return bool(self.prompt is not None or self.responses or self.user_files)


def canvas_of(item):
    title = (item.get("title") or "")[len("Created Gemini Canvas titled "):].strip()
    content = ""
    for s in item.get("subtitles") or []:
        content = s.get("name") or ""
        break
    return {"time": item.get("time"), "title": title, "content": content}


def group(items):
    """Return {conversation id: [Turn, ...]} and a list of canvases with no conversation."""
    convs, prompts, canvases = {}, [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or ""
        if title.startswith("Created Gemini Canvas titled"):
            canvases.append(canvas_of(it))
            continue
        det = it.get("details") or []
        if not det:
            continue  # feedback, survey, "Used Gemini Apps", ...
        turn = Turn(it)
        if not turn.has_content():
            continue
        cid = conv_id(det[0].get("url") or det[0].get("name"))
        convs.setdefault(cid, []).append(turn)
        if turn.prompt is not None:
            prompts.append((sort_key(turn.time), turn))

    prompts.sort(key=lambda p: p[0])
    keys = [p[0] for p in prompts]
    orphans = []
    import bisect
    for cv in canvases:
        t = sort_key(cv["time"])
        i = bisect.bisect_right(keys, t) - 1
        if i >= 0 and t - keys[i] <= CANVAS_WINDOW:
            prompts[i][1].canvases.append(cv)
        else:
            orphans.append(cv)
    for turns in convs.values():
        turns.sort(key=lambda tr: sort_key(tr.time))
    return convs, orphans


# ================================================================ input: zip, folder or file
class Source:
    """MyActivity.json and the files beside it, read from a folder or from Takeout zips."""

    def __init__(self, path):
        path = Path(path).expanduser()
        self.zips, self.members, self.folder = [], {}, None
        if path.is_file() and zipfile.is_zipfile(path):
            self._open_zips(path)
        elif path.is_dir():
            self.folder = self._find_json(path).parent
        elif path.is_file():
            self.folder = path.parent
            self.json_name = path.name
        else:
            raise SystemExit(f"Not found: {path}")
        self.path = path

    # -- zips
    def _open_zips(self, first):
        m = re.match(r"^(.*-)\d+-\d{3}\.zip$", first.name)
        parts = sorted(first.parent.glob(glob_escape(m.group(1)) + "*-[0-9][0-9][0-9].zip")) if m else []
        parts = parts if first in parts else [first] + parts
        candidates = []
        for zp in parts:
            if not zipfile.is_zipfile(zp):
                continue
            z = zipfile.ZipFile(zp)
            self.zips.append(z)
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                if name.endswith("/MyActivity.json"):
                    candidates.append((z, name))
                self.members.setdefault(name, z)
        json_member = self._pick(candidates, lambda c: c[1])
        self.json_zip, self.json_member = json_member
        self.prefix = json_member[1].rsplit("/", 1)[0] + "/"

    @staticmethod
    def _pick(candidates, name_of):
        if not candidates:
            raise SystemExit("No MyActivity.json found. Export Gemini Apps activity in JSON format.")
        gemini = [c for c in candidates if "/Gemini Apps/" in "/" + name_of(c)]
        if len(gemini) == 1 or (not gemini and len(candidates) == 1):
            return (gemini or candidates)[0]
        raise SystemExit("Found several MyActivity.json files; pass the one you want:\n  "
                         + "\n  ".join(name_of(c) for c in (gemini or candidates)))

    # -- folders
    def _find_json(self, folder):
        direct = folder / "MyActivity.json"
        if direct.exists():
            self.json_name = "MyActivity.json"
            return direct
        found = self._pick(sorted(folder.rglob("MyActivity.json")), lambda p: str(p))
        self.json_name = found.name
        return found

    # -- access
    def load_json(self):
        if self.zips:
            with self.json_zip.open(self.json_member) as f:
                return json.load(f)
        with (self.folder / self.json_name).open(encoding="utf-8") as f:
            return json.load(f)

    def default_out(self):
        if self.zips:
            stem = re.sub(r"-\d+-\d{3}$", "", self.path.stem)
            return self.path.with_name(stem + "_md")
        return self.folder / "MyActivity_md"

    def has(self, name):
        if self.zips:
            return self.prefix + name in self.members
        return (self.folder / name).exists()

    def link_target(self, name, out_dir):
        """Relative link from out_dir to the file, extracting it first when reading a zip."""
        if self.zips:
            dest = out_dir / "files" / name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with self.members[self.prefix + name].open(self.prefix + name) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
            return "files/" + quote(name)
        return quote(os.path.relpath(self.folder / name, out_dir).replace(os.sep, "/"))


def glob_escape(s):
    return re.sub(r"([*?\[])", r"[\1]", s)


# ================================================================ rendering
class Ctx:
    def __init__(self, source, out_dir, args):
        self.source, self.out_dir, self.args = source, Path(out_dir), args
        self.canvas_names = set()

    def file_link(self, name, path, image=False):
        if "/" in path or "\\" in path or not self.source.has(path):
            return f"`{name}`"
        rel = self.source.link_target(path, self.out_dir)
        return f"![{name}]({rel})" if image else f"[{name}]({rel})"

    def save_canvas(self, cv, lang, body, conv_title):
        ext = {"html": ".html", "python": ".py", "javascript": ".js", "js": ".js",
               "markdown": ".md", "md": ".md", "css": ".css", "json": ".json"}.get(lang.lower(), ".txt")
        d = to_dt(cv["time"])
        stamp = d.strftime("%Y-%m-%d %H%M%S") if d else "undated"
        base = f"{stamp} {safe_filename(cv['title'] or conv_title, 60)}"
        name = base + ext
        k = 2
        while name.lower() in self.canvas_names:
            name, k = f"{base} ({k}){ext}", k + 1
        self.canvas_names.add(name.lower())
        folder = self.out_dir / "canvas"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(body, encoding="utf-8")
        return "canvas/" + quote(name)


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".svg")


def render_canvas(cv, ctx, conv_title):
    title, content = cv["title"], (cv["content"] or "").strip()
    label = f"Canvas: {title}" if title and not PLACEHOLDER.match(title) else "Canvas"
    m = re.match(r"^```([\w+#.-]*)[^\n]*\n(.*?)\n?```\s*$", content, re.S)
    if m:  # app or code canvas: save it to its own file
        lang = m.group(1) or "txt"
        body = m.group(2)
        inner = re.match(r"^```([\w+#.-]*)[^\n]*\n(.*)$", body, re.S)  # occasional doubled fence
        if inner and lang == "html" and not body.lstrip().startswith("<"):
            lang, body = inner.group(1) or "txt", inner.group(2)
        if body.lstrip()[:1] in "{[" and lang not in ("html", "python", "javascript", "js"):
            lang = "json"  # quiz and flashcard canvases are JSON
        if label == "Canvas" and "<title>Generated widgets</title>" in body[:3000]:
            label = "Interactive widget"
        link = ctx.save_canvas(cv, lang, body, conv_title)
        return f"*[{label}]({link}) ({lang}, {len(body):,} characters)*"
    if "googleusercontent.com/youtube_content" in content:
        return f"*[YouTube video: {title}]*" if title and not PLACEHOLDER.match(title) else "*[YouTube video]*"
    if not content or PLACEHOLDER.match(content) or content.startswith("[Image of "):
        if label == "Canvas" or PLACEHOLDER.match(title or ""):
            return ""  # map or image placeholder with nothing to show
        return f"*[{label} (content not in the export)]*"
    return details(label, content)


def render_turn(turn, ctx, conv_title):
    """Return (user block or None, assistant block or None)."""
    args = ctx.args
    user = None
    if turn.prompt is not None or turn.user_files:
        lines = []
        if turn.note:
            lines.append(f"*{turn.note}*")
        if turn.context_links:
            lines.append("*Context: " + ", ".join(f"<{u}>" for u in turn.context_links) + "*")
        if turn.user_files:
            lines.append("*Attachments: " + ", ".join(ctx.file_link(n, p) for n, p in turn.user_files) + "*")
        att = "\n\n".join(lines)
        user = message_block("User", (turn.prompt or "").strip(), turn.time, att, args.timestamps)

    parts = []
    if turn.prompt is None and turn.note:
        parts.append(f"*{turn.note}*")
    if turn.responses:
        parts.append(html_to_md(turn.responses[0]))
    for f in turn.generated:
        parts.append(ctx.file_link(f, f, image=f.lower().endswith(IMAGE_EXT)))
    if not turn.generated:
        parts += [f"*{x}*" for x in turn.extra]
    for k, h in enumerate(turn.responses[1:], 2):
        parts.append(details(f"Other draft {k}", html_to_md(h)))
    for cv in turn.canvases:
        parts.append(render_canvas(cv, ctx, conv_title))
    parts = [p for p in parts if p and p.strip()]
    if not parts and turn.prompt is not None:
        parts = ["*[No response in the export.]*"]
    bot = message_block("Gemini", "\n\n".join(parts), turn.time, "", args.timestamps) if parts else None
    return user, bot


def convert(cid, turns, title, ctx):
    blocks, n = [], 0
    for turn in turns:
        for b in render_turn(turn, ctx, title):
            if b:
                blocks.append(b)
                n += 1
    created, updated = turns[0].time, turns[-1].time
    front = [
        ("title", title),
        ("id", cid),
        ("url", f"https://gemini.google.com/app/{cid}" if cid else ""),
        ("created", fmt_dt(created)),
        ("updated", fmt_dt(updated)),
    ]
    return document(title, front, blocks), n, created


# ================================================================ main
def main():
    ap = base_parser("Convert a Google Takeout Gemini Apps activity export (MyActivity.json) to Markdown files.",
                     "Takeout zip, Takeout folder, or MyActivity.json", tool_options=False)
    ap.add_argument("--titles", action="append", metavar="DIR",
                    help="Folder of saved Gemini Markdown pages to take conversation titles from")
    args = ap.parse_args()
    set_timezone(args.tz)

    source = Source(args.input)
    data = source.load_json()
    if not isinstance(data, list):
        raise SystemExit("Unrecognized JSON layout: expected the list of activity entries in MyActivity.json.")
    writer = Writer(Path(args.out).expanduser() if args.out else source.default_out(), "Gemini conversations")
    ctx = Ctx(source, writer.out_dir, args)
    titles = load_titles(args.titles)

    convs, orphans = group(data)
    for cid, turns in convs.items():
        first = next((t.prompt for t in turns if t.prompt), "") or "Untitled"
        title = titles.get(cid) or short_title(first)
        try:
            md, n, created = convert(cid, turns, title, ctx)
        except Exception as e:  # keep going on one bad conversation
            writer.fail(title, cid, f"conversion error: {e}")
            continue
        writer.add(md, title, cid, created, n)

    for cv in orphans:  # canvases with no prompt shortly before them
        title = f"Canvas: {cv['title']}" if cv["title"] and not PLACEHOLDER.match(cv["title"]) else "Canvas"
        body = render_canvas(cv, ctx, title)
        if not body:
            continue
        md = document(title, [("title", title), ("id", ""), ("url", ""), ("created", fmt_dt(cv["time"])),
                              ("updated", fmt_dt(cv["time"]))],
                      [message_block("Gemini", body, cv["time"], "", args.timestamps)])
        writer.add(md, title, "", cv["time"], 1)
    writer.finish()


if __name__ == "__main__":
    main()
