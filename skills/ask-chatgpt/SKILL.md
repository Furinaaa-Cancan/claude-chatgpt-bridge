---
name: ask-chatgpt
description: Send a prompt (optionally with a file such as a .zip) to the user's ChatGPT desktop app and read back the complete answer as Markdown, by driving the app's UI through Peekaboo. Use only when the user explicitly asks to consult ChatGPT / GPT Pro, or when the chatgpt-review skill calls for it — never on your own initiative.
argument-hint: "[question for ChatGPT]"
---

# Ask ChatGPT (desktop app via Peekaboo)

`~/.claude/skills/ask-chatgpt/chatgpt_bridge.py` drives the ChatGPT desktop app the way a person would: it
opens a chat, types the prompt, optionally uploads a file, clicks Send, waits for the reply, and reads it
back through the reply's Copy button. It uses the user's own ChatGPT account and whatever model is selected
in the app.

## Before you run it

- **Tell the user first.** At the moments it clicks (New chat, upload, Send, Copy) it brings ChatGPT to the
  front and moves the mouse; if the user is typing or clicking elsewhere at that moment the step can fail.
  The long wait in between happens in the background and does not disturb them.
- **The prompt must be self-contained.** ChatGPT sees nothing of this conversation or the codebase except
  what you put in the prompt or the attachment.
- **Never send secrets** (keys, tokens, passwords, `.env` contents, private keys). Before the first upload of
  the user's code or private documents in a session, confirm they are fine with it leaving the machine.
- Requirements: ChatGPT desktop app running with its window open; Peekaboo.app running (it holds the Screen
  Recording + Accessibility grants). `status` checks both.

## Commands

```bash
B=~/.claude/skills/ask-chatgpt/chatgpt_bridge.py
python3 $B status                                   # window title, busy?, draft?, ledger usage
python3 $B ask - --new-chat --out /path/answer.md < prompt.md
python3 $B ask - --new-chat --attach project.zip --out /path/answer.md < prompt.md
python3 $B ask - --conversation "Exact title" --out /path/answer.md < prompt.md   # follow-up
python3 $B read-last --out /path/answer.md          # re-copy the latest answer (no new message)
python3 $B wait --conversation "Exact title" --out /path/answer.md   # take over a reply still in progress
python3 $B usage ["Exact title"]                    # token ledger
```

- Always write the prompt to a file and pass it on stdin (`-`); multi-line prompts survive intact.
- `--out` also writes `<out>.meta.json` with the conversation `title`, `tokens_used`, `budget`, `remaining`.
  Keep the title if you may follow up.
- Run `ask` with the Bash tool's `run_in_background: true` and a generous `--timeout` (default 1800 s; use
  10800 with `--poll 10` for long reviews — Pro can work for 30–60+ minutes). You are notified when it
  finishes; do not poll. The run is pinned to its conversation: if the user opens another chat meanwhile,
  it waits and switches back once ChatGPT has been idle for two minutes.

## Choosing the conversation

- Default to `--new-chat`. Add `--title "<unique title>"` when you may need the chat again (follow-ups,
  long runs): ChatGPT renames auto-titled chats during and after answering, and auto titles repeat.
- For long or unattended runs also pass `--marker <unique id>`, a string that appears in the prompt and
  that the answer will echo (e.g. a batch id). ChatGPT may replace even a title you set once the first
  answer completes; the marker lets the run find its chat again (by its message on screen, or by copying
  an answer and checking for the marker), and an answer that does not contain it is never written.
- Use `--conversation TITLE` only for a conversation this skill created (the title from a previous
  `.meta.json`). It switches to that chat through the sidebar and refuses if the title does not match.
- Every conversation has a token ledger (`~/.chatgpt-bridge/ledger.json`, deliberately high estimates of
  prompts + answers; an attachment counts as its text size capped at 30 000, since ChatGPT works through
  archives with its tools instead of loading them whole — `CHATGPT_BRIDGE_ATTACH_TOKENS` changes the cap). Before a follow-up, `ask` checks the budget (default 90 000 tokens,
  about 70% of the documented 128K input window; override with `--budget` or `CHATGPT_BRIDGE_BUDGET`).
  **Exit code 3** means the follow-up would exceed it: start a `--new-chat` instead and include a handoff
  summary of everything the new chat needs to know.

## When something fails

- **Never resend automatically.** A failed run may still have sent the message; resending duplicates it.
  Run `status` first.
- "Clicked Copy but the clipboard never changed" → the user was probably using the mouse/keyboard. Wait for
  them, then `read-last`.
- Timeout, or the `ask` process died while ChatGPT kept working → `status`; if it is still generating, run
  `wait --conversation "<title>"` (background) to collect the reply when it finishes; if it already finished,
  `read-last`.
- You never need to check whether ChatGPT is free first. ChatGPT generates several chats in parallel, so
  runs (from this or other Claude Code sessions) each use their own chat and wait in parallel; only the few
  seconds of clicking, uploading, sending, switching and copying are serialised by a lock. If the user has a
  draft in the composer, `ask` waits until it has been gone for a minute (`--idle-quiet`, `--idle-timeout`;
  `--no-wait-idle` fails at once instead). Run it in the background.
- "is not visible in the sidebar" → the conversation was renamed or scrolled out of the sidebar list. With
  `--marker` the run searches the recent chats for it; otherwise use a new chat.

## Presenting the answer

Quote or summarize it as ChatGPT's view, and add your own assessment. Do not adopt its claims about the
user's code without checking them against the code.
