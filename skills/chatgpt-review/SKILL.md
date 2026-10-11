---
name: chatgpt-review
description: Iterative code-review loop with ChatGPT Pro in the desktop app - upload the code as a zip, let Pro run a long multi-pass review with its own sandbox tools and reproductions, verify every finding against the code, produce an HTML report that lists each change to make, apply the approved fixes, and re-review until clean. Use when the user asks for a ChatGPT / GPT Pro code review or a review loop.
argument-hint: "[project path or review zip] [focus or 'auto']"
---

# ChatGPT code-review loop

Built on the `ask-chatgpt` skill. Follow its rules: tell the user before driving ChatGPT (it briefly takes
the mouse at upload / send / copy), never resend automatically, never upload secrets.

```bash
RV=~/.claude/skills/chatgpt-review/review.py      # state, chunked packing, prompts, ingest, HTML report
B=~/.claude/skills/ask-chatgpt/chatgpt_bridge.py   # talks to ChatGPT
```

State lives outside the project in `~/.chatgpt-review/<name>-<hash>/` (`review.py status` prints it):
`state.json`, `round-N/part-K/{zip,prompt.md,answer.md}`, `round-N/report.html`, latest `report.html`.
Nothing is written into the user's repository.

## How ChatGPT reviews

By default the whole code base goes to ChatGPT as **one package in one conversation**. Pro unpacks it in its
sandbox and works through it with its own tools (scans, reading along call chains, small reproduction
scripts) — often 30–60 minutes for a few hundred files — so the zip does not have to fit the context window,
and cross-module problems stay visible. The prompt asks for three passes (find by module → reproduce along
call chains → cross-check and withdraw false positives) and for an honest coverage statement.

`pack --chunk-tokens N` splits the code into parts reviewed in separate conversations. Use it only as a
fallback, e.g. when ChatGPT reports large areas it could not read; part numbers stay stable across rounds.

## Set up (once per review)

- **Working tree**: read enough of the project to write a 3–6 sentence context (purpose, stack, entry
  points, constraints), then `python3 $RV init --root <project> --context "<context>" [--focus "<focus>"]`.
- **Prepared package** (a zip the user built, e.g. a commit snapshot):
  `python3 $RV init --root <project> --source-zip <zip>`. A `00_审查说明.md` / `REVIEW_BRIEF.md` inside it is
  used as the review brief (replacing context/focus), and `改动.diff` / `CHANGES.diff` is split per file into
  each part. `--brief FILE` / `--diff FILE` set them explicitly.

Then `python3 $RV pack --root <project>` and show the user the plan: files, zip size, anything excluded as a
possible secret. Tell them a round can take 30–60+ minutes, that they can keep using the computer (and even
ChatGPT — the bridge waits and returns to the review conversation when ChatGPT is idle), and confirm before
the first upload of their code.

## Running a round

Repeat until `next` says `"done": true`:

1. `python3 $RV next --root <project>` → JSON with `part`, `zip`, `prompt`, `out`, `conversation`.
2. Send it (Bash, `run_in_background: true`):
   - `conversation` is null → `python3 $B ask - --new-chat --attach <zip> --timeout 10800 --poll 10 --out <out> < <prompt>`
   - otherwise → same but `--conversation "<conversation>"`. On **exit code 3** (context budget):
     `python3 $RV prompt --root <project> --part <part> --handoff`, then send with `--new-chat`.
3. While ChatGPT works (chunked mode only), verify the findings of the previous part (step 5) — only one
   ChatGPT request runs at a time.
4. When it finishes: `python3 $RV ingest <out> --root <project> --part <part>`. If it reports no JSON, ask in
   the same conversation "请只重新输出上一条回答末尾的 JSON 代码块，不要其他内容。" and ingest that answer.
   If it fails mid-way, follow ask-chatgpt's failure rules (`status`, `read-last`), never blind resend.
5. **Verify every new finding yourself** against the source (`status` shows the source root; for a
   `--source-zip` review that is the extracted snapshot). Check the evidence is really there and whether the
   problem is real in context. Then
   `python3 $RV set F3 --verdict confirmed|disputed|uncertain --note "<具体理由，引用代码>" --root <project>`.
   A dispute must cite code; "uncertain" says what would settle it.

After the last part: `python3 $RV report --root <project> --summary "<你的总结：几条成立、最大风险、建议先改什么>"`,
send the HTML to the user (SendUserFile, display "render") and list the items to change in chat.
`report` can also be run mid-round to show progress (it warns which parts are not done yet).

## Fixing and the next round

1. Default: ask the user which items to fix (suggest all confirmed critical/high/medium). If they asked for
   an automatic loop ("auto" / "自动"), fix every confirmed item of medium or higher without asking. Never
   commit or push unless asked.
2. Fixes go into the real project. For a `--source-zip` review, map snapshot paths to the working tree and
   tell the user if a file has moved or changed since the snapshot; the next round then needs a fresh
   package (re-`init` with the new zip, or switch to the working tree).
3. Record outcomes: `set F1 F2 --status fixed --note "<怎么改的>"`; items left as is:
   `--status wontfix|deferred --note "<理由>"` (ChatGPT sees these reasons next round).
4. `pack` again → only changed / pending parts are sent → run the round as above.

## When to stop

After a round with no open confirmed finding of medium or higher and all fixes verified; after 3 rounds
unless the user set another limit; or when the user says stop. Finish with a final report and summary:
rounds, parts, issues found / fixed / declined, anything still open, files ChatGPT said it could not read.
