"""
Shared helpers for the chat-export to Markdown converters
(chatgpt_json_to_md.py, librechat_json_to_md.py, gemini_json_to_md.py,
perplexity_json_to_md.py).

Only the Python standard library is needed (Python 3.8+; --tz needs 3.9+).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

TZ = None  # None means the computer's local time zone


# ---------------------------------------------------------------- dates
def set_timezone(name):
    global TZ
    if not name:
        return
    if not ZoneInfo:
        sys.exit("--tz needs Python 3.9 or newer.")
    TZ = ZoneInfo(name)


def to_dt(ts):
    """Parse a Unix timestamp (number or numeric string) or an ISO 8601 string."""
    if ts in (None, ""):
        return None
    try:
        if isinstance(ts, str):
            try:
                ts = float(ts)
            except ValueError:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(TZ)
    except (ValueError, OSError, OverflowError):
        return None


def fmt_dt(ts, with_time=True):
    d = to_dt(ts)
    if not d:
        return ""
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def sort_key(ts):
    d = to_dt(ts)
    return d.timestamp() if d else 0


# ---------------------------------------------------------------- text helpers
def safe_filename(title, max_len=80):
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title or "Untitled")
    t = re.sub(r"\s+", " ", t).strip().strip(".")
    return (t[:max_len].rstrip() or "Untitled")


def short_title(text, max_len=100):
    """First line of a prompt, whitespace collapsed, cut at a word boundary."""
    t = re.sub(r"\s+", " ", (text or "").strip().split("\n")[0]).strip()
    if len(t) <= max_len:
        return t or "Untitled"
    cut = t[:max_len].rsplit(" ", 1)[0]
    return (cut if len(cut) > max_len // 2 else t[:max_len]).rstrip(" ,;:-") + "…"


def yaml_str(s):
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def fence(text, lang=""):
    """Code fence that cannot be broken by backticks inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{(text or '').rstrip()}\n{ticks}"


def details(summary, body):
    """Collapsible block (renders in GitHub, Obsidian, VS Code preview)."""
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>" if body else ""


def attachments_line(names):
    names = [n for n in names if n]
    return ("*Attachments: " + ", ".join(f"`{n}`" for n in names) + "*") if names else ""


# ---------------------------------------------------------------- document assembly
def message_block(label, body, when=None, att="", timestamps=False):
    """Heading, optional timestamp, attachments and body for one message."""
    head = f"## {label}"
    if timestamps and when:
        head += f"\n\n*{fmt_dt(when)}*"
    return "\n\n".join(x for x in (head, att, body) if x)


def document(title, front, blocks):
    """YAML front matter, title heading and message blocks.

    front: list of (key, value); empty values are left out, except the keys in
    ALWAYS_KEYS, which are written even when blank so every file has the same header.
    """
    lines = ["---"]
    for k, v in front:
        if v not in (None, "") or k in ALWAYS_KEYS:
            lines.append(f"{k}: {yaml_str(v)}")
    lines += ["---", "", f"# {title}", "", ""]
    return "\n".join(lines) + "\n\n".join(blocks).rstrip() + "\n"


ALWAYS_KEYS = {"title", "id", "url", "created", "updated"}


# ---------------------------------------------------------------- command line
def base_parser(description, input_help, multiple_inputs=False, tool_options=True):
    ap = argparse.ArgumentParser(description=description)
    if multiple_inputs:
        ap.add_argument("inputs", nargs="+", help=input_help)
    else:
        ap.add_argument("input", help=input_help)
    ap.add_argument("-o", "--out", help="Output folder")
    if tool_options:  # exports that record tool calls and thinking
        ap.add_argument("--include-tools", action="store_true", help="Include tool / browsing / code output")
        ap.add_argument("--include-thinking", action="store_true", help="Include reasoning (thinking) blocks")
    ap.add_argument("--timestamps", action="store_true", help="Timestamp each message")
    ap.add_argument("--tz", help="Time zone, e.g. America/New_York")
    return ap


# ---------------------------------------------------------------- output
class Writer:
    """Writes one Markdown file per conversation plus an _index.md listing them."""

    def __init__(self, out_dir, heading):
        self.out_dir = Path(out_dir).expanduser()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.heading = heading
        self.index, self.failed, self.used = [], [], set()

    def add(self, md, title, cid, created, n_msgs):
        date = fmt_dt(created, with_time=False) or "undated"
        base = f"{date} {safe_filename(title)}"
        name = base + ".md"
        if name.lower() in self.used:
            name = f"{base} ({(cid or str(len(self.used)))[:8]}).md"
        self.used.add(name.lower())
        (self.out_dir / name).write_text(md, encoding="utf-8")
        self.index.append((sort_key(created), date, title, name, n_msgs))

    def fail(self, title, cid, reason):
        self.failed.append((title, cid, reason))

    def finish(self):
        self.index.sort(key=lambda r: r[0], reverse=True)
        lines = [f"# {self.heading} ({len(self.index)})", ""]
        for _, date, title, name, n in self.index:
            link = name.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            lines.append(f"- {date}: [{title}]({link}) ({n} messages)")
        if self.failed:
            lines += ["", f"## Not converted ({len(self.failed)})", ""]
            lines += [f"- {t} (`{i}`): {e}" for t, i, e in self.failed]
        (self.out_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"Wrote {len(self.index)} conversations to {self.out_dir}")
        if self.failed:
            print(f"{len(self.failed)} could not be converted (listed in _index.md):")
            for t, _, e in self.failed:
                print(f"  - {t}: {e}")
