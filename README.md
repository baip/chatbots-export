# Chatbot export scripts

Scripts to export chat history from ChatGPT, LibreChat (genai.umass.edu) and Claude (claude.ai), and to convert those exports (including Claude's data export), Google Takeout's Gemini Apps activity and Perplexity's data export to Markdown.

| File | What it does |
|---|---|
| `chatgpt_export.js` | Paste into the DevTools Console on chatgpt.com. Downloads every conversation as one JSON file (from [hoya98/chatgpt-export](https://github.com/hoya98/chatgpt-export), MIT license). |
| `librechat_export_json.js` | Paste into the DevTools Console on genai.umass.edu. Exports every chat in the sidebar (or the IDs in `ONLY_IDS`) as one JSON file per chat, through the site's Share > Export dialog. Waits for each chat to load and blocks empty exports. |
| `claude_download_export.js` | Paste into the DevTools Console on claude.ai (logged in). Pick the `manifest-*.json` from Claude's data-export email and it downloads every zip listed in it. Each link works once and expires after 24 hours. Set `ONLY_FILES` to download only some files. |
| `chatgpt_json_to_md.py` | Converts the ChatGPT export to Markdown. |
| `librechat_json_to_md.py` | Converts LibreChat JSON exports to Markdown. |
| `gemini_json_to_md.py` | Converts a Google Takeout Gemini Apps export to Markdown. Takes the Takeout zip, the extracted folder, or `MyActivity.json`. |
| `perplexity_json_to_md.py` | Converts a Perplexity data export (the emailed zip, or the `conversations-*.json` inside it) to Markdown. |
| `claude_json_to_md.py` | Converts a claude.ai data export to Markdown. Takes the folder of downloaded zips, any of the zips, or the extracted JSON. Also writes projects (instructions and knowledge files) and memory. |
| `md_common.py` | Helpers shared by all converters (dates, file names, front matter, `_index.md`, command-line options). Keep it in the same folder as the converters. |

## Converting to Markdown

```bash
python3 chatgpt_json_to_md.py chatgpt-export-2026-10-14.json
python3 librechat_json_to_md.py ~/Downloads/librechat-json
python3 gemini_json_to_md.py takeout-xudd09-20261015T034159Z-1-001.zip
python3 perplexity_json_to_md.py user_data_export_2026-10-15-03-21-27_....zip
python3 claude_json_to_md.py ~/Downloads/claude-export
```

All converters write one `.md` file per conversation, named `YYYY-MM-DD Title.md`, plus `_index.md`, which lists the conversations newest first and any that could not be converted. Each file starts with YAML front matter (`title`, `id`, `url`, `created`, `updated`, `model`).

Shared options:

| Option | Effect |
|---|---|
| `-o DIR` | Output folder (default: `<input>_md` next to the input) |
| `--include-tools` | Include tool calls and their output (ChatGPT, LibreChat, Claude) |
| `--include-thinking` | Include the model's thinking as collapsible blocks (ChatGPT, LibreChat, Claude) |
| `--timestamps` | Add a timestamp under each message heading |
| `--tz ZONE` | Time zone for dates, for example `America/New_York` |

LibreChat only: `--all-branches` (append the other branches of a chat), `--full-tool-output` (do not shorten tool output to 3,000 characters), `--base-url` (site address for the `url` field).

Gemini only: `--titles DIR` takes conversation titles from Markdown pages saved from gemini.google.com (files with a `Source: https://gemini.google.com/app/<id>` line); otherwise each conversation is named after its first prompt, because Takeout records no titles. Takeout logs one entry per prompt, so the converter groups entries by their conversation link. Document canvases are included inline, app and code canvases are saved to a `canvas/` subfolder and linked, and attached or generated files are linked. When the input is a zip, the files used are extracted into a `files/` subfolder of the output, so the output folder is self-contained. When the input is a folder or `MyActivity.json`, they link to the originals with relative paths, which break if either folder is moved. For a Takeout split into several zips (`-1-001.zip`, `-1-002.zip`, ...), pass any one part; the others in the same folder are read too. Takeout does not record the model.

Claude only: `--all-branches` (append the other branches of a conversation, from edited prompts and retried replies; by default only the branch with the newest message is kept, because the export does not record which branch was on screen), `--full-tool-output` (do not shorten tool output to 3,000 characters), `--strip-citations` (leave out the source links added after cited sentences, which the converter titles from the conversation's search results), `--include-summary` (start each file with Claude's summary of the conversation). Artifacts, message-composer drafts, image search results and the names of shared files are always kept. Projects go to `projects/<name>/` (`_project.md` plus each knowledge file) and memory to `memory/` (`memory.md` plus the memory files), both linked from `_index.md`. The export has file names but not the uploaded or generated files themselves, and no model name.

Perplexity only: `--strip-citations` removes the `[1]` markers, since the export does not include the sources they refer to. The export also has no thread links, so `url` is blank.

Only the Python standard library is needed (Python 3.8+, or 3.9+ for `--tz`).
