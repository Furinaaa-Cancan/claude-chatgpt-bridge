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
python3 $B wait --conversation "Exact title" --marker "bridge-run-<receipt-id>" --out /path/answer.md   # take over a reply still in progress
python3 $B usage ["Exact title"]                    # token ledger
```

- Always write the prompt to a file and pass it on stdin (`-`); multi-line prompts survive intact.
- `ask --attach` checks the on-disk file size before starting Peekaboo or reading attachment contents,
  and rechecks the staged upload copy. Maximum: **512 MiB (536,870,912 bytes)** per file; a ZIP is
  measured compressed. Larger files fail with exit code **1** and a size error. Reduce or split the
  attachment; changing token budgets cannot fix this. Account or file-type limits may be lower.
  Passing this check guarantees neither upload success nor complete review coverage.
- `--out` also writes `<out>.meta.json` with the conversation `title`, `marker`, `tokens_used`, `budget`, `remaining`.
  The token fields are local estimates, not ChatGPT usage telemetry. Keep the title if you may follow up.
- Run `ask` with the Bash tool's `run_in_background: true` and a generous `--timeout` (default 1800 s; use
  10800 with `--poll 10` for long reviews — Pro can work for 30–60+ minutes). You are notified when it
  finishes; do not poll. The run is pinned to its conversation: if the user opens another chat meanwhile,
  it waits and switches back once ChatGPT has been idle for two minutes.

## Choosing the conversation

- Default to `--new-chat`. Add `--title "<unique title>"` when you may need the chat again (follow-ups,
  long runs): ChatGPT renames auto-titled chats during and after answering, and auto titles repeat.
- Every `ask` generates a fresh `bridge-run-...` identity and appends an instruction to echo it
  in the answer (inside `summary` for JSON). No caller-supplied marker is needed for `ask`.
  The receipt is written to `~/.chatgpt-bridge/requests/<identity>.json` before sending; the log
  prints its path. Keep it for recovery. `wait` requires `--marker` with that exact identity.
- Before uploading or entering a prompt, the target must be confirmed as Chat mode and either an
  empty new chat or the conversation containing the previous verified identity. A matching title
  alone is insufficient. Unknown mode, ambiguous controls, changed prompt whitespace, or missing
  identity stop the run. The answer must contain the current identity before it is returned.
- Use `--conversation TITLE` only with a ledger entry containing a verified marker. Older ledger
  entries without identity, or titles marked ambiguous after a collision, cannot safely identify a target; start a new chat with a handoff.
- Every conversation has a heuristic token ledger (`~/.chatgpt-bridge/ledger.json`): prompts + answers,
  plus attachment text estimates capped at 30 000 (`CHATGPT_BRIDGE_ATTACH_TOKENS` changes the cap).
  The bridge cannot observe ChatGPT's internal file reads, tool output, or actual context usage.
  Neither this cap nor the default 90 000 token budget is an official limit or a safe upper bound on
  usage. Before a follow-up, `ask` checks this local budget (`--budget` or `CHATGPT_BRIDGE_BUDGET`).
  These settings are independent of the attachment's upload byte limit.
  **Exit code 3** means the follow-up would exceed it: start a `--new-chat` instead and include a handoff
  summary of everything the new chat needs to know.

## Switching it off, and what is running

- `python3 $B status` shows whether the bridge is switched on, whether Peekaboo is running, and every run
  that is using the screen (from any session: pid, command, what it is for, since when). It never starts
  Peekaboo just to look.
- `python3 $B stop` switches the bridge off for every run in every session: runs stop before their next
  click or key press and waiting runs end at their next check. It then waits until every registered run
  has really ended and quits Peekaboo; anything still alive is listed (`stop --force` ends it). While
  switched off, commands exit with code 5 without touching the screen. `python3 $B resume` switches it
  back on. When the user wants the screen back, use `stop`; do not hunt for processes.
- Runs start Peekaboo.app only when they need the screen and quit it when the last run that needed it ends.
- Runs click or type only after the mouse and keyboard have been idle for `CHATGPT_BRIDGE_USER_IDLE` seconds
  (default 30, 0 explicitly disables idle waiting). Input attribution is unknown, so the bridge's
  own input also resets this quiet period. This is deliberately slower; a failed idle query stops
  UI input. Click targets are re-read after waiting and focusing, and ambiguous targets are rejected.
- A run that cannot find its chat gives up after 3 searches. Searching only looks at other chats (and
  scrolls up to the run's own message); it never copies another chat's answer.

## When something fails

- **Never resend automatically.** A receipt in `sending` or `delivery_unknown` means delivery is
  uncertain. Inspect the chat and recover the answer with `wait --conversation ... --marker ...`.
  `sent` confirms the request marker was observed in the conversation; `completed` means its answer
  was collected. Do not convert a missing acknowledgment into an automatic retry.
- "Clicked Copy but the clipboard never changed" → the user was using the mouse/keyboard, or ChatGPT left the
  answer's Copy button stuck as "Copied" (it can, once an answer has been copied). The bridge then copies the
  answer's last code block instead, so ask for structured results inside a code block; otherwise wait, then
  `read-last`.
- Timeout, or the `ask` process died while ChatGPT kept working → `status`; if it is still generating, run
  `wait --conversation "<title>" --marker "<receipt marker>"` (background) to collect and verify the reply, including when it has already finished. `read-last` reads the manually selected visible chat without task-identity verification; do not use it as automatic recovery.
- You never need to check whether ChatGPT is free first. ChatGPT generates several chats in parallel, so
  runs (from this or other Claude Code sessions) each use their own chat and wait in parallel; the
  UI operations (clicking, uploading, sending, switching, scrolling and copying) are serialised by a lock. If the user has a
  draft in the composer, `ask` waits until it has been gone for a minute (`--idle-quiet`, `--idle-timeout`;
  `--no-wait-idle` fails at once instead). Run it in the background.
- "is not visible in the sidebar" → the conversation was renamed or scrolled out of the sidebar list. The run uses its automatic identity to search recent chats; it stops if the identity cannot be verified.

## Presenting the answer

Quote or summarize it as ChatGPT's view, and add your own assessment. Do not adopt its claims about the
user's code without checking them against the code.
