# Chatbot export scripts

Scripts to export chat history from ChatGPT and LibreChat (genai.umass.edu) and convert it to Markdown.

| File | What it does |
|---|---|
| `chatgpt_export.js` | Paste into the DevTools Console on chatgpt.com. Downloads every conversation as one JSON file (from [hoya98/chatgpt-export](https://github.com/hoya98/chatgpt-export), MIT license). |
| `librechat_export_json.js` | Paste into the DevTools Console on genai.umass.edu. Exports every chat in the sidebar (or the IDs in `ONLY_IDS`) as one JSON file per chat, through the site's Share > Export dialog. Waits for each chat to load and blocks empty exports. |
| `chatgpt_json_to_md.py` | Converts the ChatGPT export to Markdown. |
| `librechat_json_to_md.py` | Converts LibreChat JSON exports to Markdown. |
| `md_common.py` | Helpers shared by both converters (dates, file names, front matter, `_index.md`, command-line options). Keep it in the same folder as the converters. |

## Converting to Markdown

```bash
python3 chatgpt_json_to_md.py chatgpt-export-2026-10-14.json
python3 librechat_json_to_md.py ~/Downloads/librechat-json
```

Both converters write one `.md` file per conversation, named `YYYY-MM-DD Title.md`, plus `_index.md`, which lists the conversations newest first and any that could not be converted. Each file starts with YAML front matter (`title`, `id`, `url`, `created`, `updated`, `model`).

Shared options:

| Option | Effect |
|---|---|
| `-o DIR` | Output folder (default: `<input>_md` next to the input) |
| `--include-tools` | Include tool calls and their output |
| `--include-thinking` | Include the model's thinking as collapsible blocks |
| `--timestamps` | Add a timestamp under each message heading |
| `--tz ZONE` | Time zone for dates, for example `America/New_York` |

LibreChat only: `--all-branches` (append the other branches of a chat), `--full-tool-output` (do not shorten tool output to 3,000 characters), `--base-url` (site address for the `url` field).

Only the Python standard library is needed (Python 3.8+, or 3.9+ for `--tz`).
