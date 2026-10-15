#!/usr/bin/env python3
"""Drive the ChatGPT desktop app through the Peekaboo CLI.

    chatgpt_bridge.py status
    chatgpt_bridge.py ask PROMPT|- (--new-chat [--title T] | --conversation TITLE) [--attach FILE]
                      [--timeout 1800] [--budget 90000] [--out answer.md]
    chatgpt_bridge.py read-last [--out answer.md]
    chatgpt_bridge.py wait --conversation TITLE --marker ID [--out answer.md]   # take over a reply in progress
    chatgpt_bridge.py usage [TITLE]
    chatgpt_bridge.py stop [--force] [--keep-peekaboo]   # switch off: every run stops, Peekaboo quits
    chatgpt_bridge.py resume                              # switch back on

Exit codes: 0 ok, 1 error, 3 the conversation's context budget would be exceeded
(start a new chat and hand the relevant context over in the prompt), 5 switched off.

Control: every run that uses the screen registers in ~/.chatgpt-bridge/runs/ (`status` lists them),
starts Peekaboo.app only when it is needed and quits it when the last run that needed it ends, waits
until the mouse and keyboard have been idle (CHATGPT_BRIDGE_USER_IDLE seconds, default 30) before it
clicks or types, and gives up after a bounded search instead of looping.

Every conversation gets a heuristic token ledger (~/.chatgpt-bridge/ledger.json).
It cannot measure ChatGPT's actual context usage or review coverage. With --out, a
<out>.meta.json records the title and estimates. Attachment upload bytes are checked
separately, before starting UI automation.

Lessons baked in (found by testing against ChatGPT 155.x, Peekaboo 4.2.2):
  * ChatGPT is Chromium-based; the default AX depth stops before the web content, so every
    `see` passes large depth/children/element limits.
  * The CLI may route to an on-demand daemon without TCC grants; the Peekaboo.app bridge
    socket (which has Screen Recording + Accessibility) is pinned.
  * AXPress on Chromium buttons blocks the bridge and window-targeted focus fails
    verification, so buttons get a real click at global coordinates from their AX bounds,
    and only after ChatGPT is confirmed frontmost (a global click hits whatever is on top).
  * The AX tree only exposes the visible part of a long answer, so answers are read through
    the Copy button + clipboard (saved and restored around it). Long answers leave a
    "Scroll to bottom" button with the action bar off screen; the view is scrolled first.
  * Composer buttons are found next to "Add files and more": attachments push that row below
    the text box. An empty new chat shows "Start new voice chat" instead of Send; while a
    reply is generated the button is "Stop".
  * The conversation title is the web area's AX name ("ChatGPT" on an empty new chat).
  * `peekaboo dialog file` cannot see the upload sheet, so uploads go through ⌘⇧G with the
    file alone in a fresh folder, then a double-click on its icon.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
import zipfile

BRIDGE = os.path.expanduser("~/Library/Application Support/Peekaboo/bridge.sock")
APP = "ChatGPT"
BUNDLE = "com.openai.codex"
AX_LIMITS = ["--depth", "80", "--max-children", "1000", "--max-elements", "20000"]
UTF8 = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
SENTINEL = "__chatgpt_bridge_sentinel__"
STATE_DIR = os.path.expanduser("~/.chatgpt-bridge")
LEDGER = os.path.join(STATE_DIR, "ledger.json")
STOP_FILE = os.path.join(STATE_DIR, "STOP")  # the off switch, shared by every run in every session
# Commands that move the mouse, press keys or change the window; reading the screen ("see") does not.
UI_ACTIONS = {"click", "press", "type", "paste", "scroll", "move", "set-value", "drag", "hotkey"}
RUNS_DIR = os.path.join(STATE_DIR, "runs")  # one file per live run: who is using the screen and why
STARTED_PEEKABOO = os.path.join(STATE_DIR, "peekaboo-started-by-bridge")
SCREEN_COMMANDS = {"ask", "wait", "read-last"}
MAX_SEARCHES = 3  # a run that cannot find its chat gives up after this many searches
USER_IDLE_DEFAULT = "30"
NEW_CHAT_TITLE = "ChatGPT"
# Local accounting policy, not the selected model's context window. Tool output and
# file reads inside ChatGPT are not observable by this bridge.
DEFAULT_BUDGET = int(os.environ.get("CHATGPT_BRIDGE_BUDGET", "90000"))
ATTACH_CONTEXT = int(os.environ.get("CHATGPT_BRIDGE_ATTACH_TOKENS", "30000"))
# Chat client 26.1007.71326: MAX_CHATGPT_UPLOAD_SIZE_BYTES; rejects size > limit.
# Account metadata may impose a lower limit. This is bytes on disk (compressed for ZIP),
# not uncompressed source size, tokens, or a guarantee of complete review coverage.
MAX_CHATGPT_UPLOAD_SIZE_BYTES = 512 * 1024 * 1024


class BudgetExceeded(Exception):
    pass


def log(msg):
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


class Stopped(Exception):
    pass


def check_stop():
    """`chatgpt_bridge.py stop` creates the switch file; every run, in any session, then stops before its next
    click or key press and waiting runs end at their next check. `resume` removes it."""
    if os.path.exists(STOP_FILE):
        raise Stopped("the bridge is switched off (run `chatgpt_bridge.py resume` to switch it back on)")


def user_idle_seconds():
    try:
        result = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("Cannot determine user idle time; refusing UI input.") from e
    m = re.search(r'"HIDIdleTime" = (\d+)', result.stdout)
    if result.returncode != 0 or not m:
        raise RuntimeError("Cannot determine user idle time; refusing UI input.")
    return int(m.group(1)) / 1e9


def wait_until_user_away():
    """Touch the screen only after nobody has used the mouse or keyboard for CHATGPT_BRIDGE_USER_IDLE seconds
    (default 30, 0 turns it off), so a run never takes over a computer someone is
    using. macOS idle time cannot reliably attribute input to a human versus this bridge;
    both reset the quiet period. Never infer ownership from similar timestamps."""
    need = float(os.environ.get("CHATGPT_BRIDGE_USER_IDLE", USER_IDLE_DEFAULT) or 0)
    announced = False
    while need:
        check_stop()
        idle = user_idle_seconds()
        if idle >= need:
            return
        if not announced:
            log(f"someone is using the computer; waiting until it has been idle for {need:.0f}s")
            announced = True
        time.sleep(5)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def active_runs(exclude=None):
    """Registered runs whose process is alive; entries left by dead processes are removed."""
    runs = []
    for name in sorted(os.listdir(RUNS_DIR)) if os.path.isdir(RUNS_DIR) else []:
        path = os.path.join(RUNS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                run = json.load(f)
        except (OSError, ValueError):
            continue
        if not _alive(run.get("pid", -1)):
            with contextlib.suppress(OSError):
                os.remove(path)
        elif run.get("pid") != exclude:
            runs.append(run)
    return runs


def peekaboo_running():
    return subprocess.run(["pgrep", "-x", "Peekaboo"], capture_output=True).returncode == 0


def start_peekaboo():
    """Peekaboo.app holds the Accessibility and Screen Recording grants; start it in the background only when
    a run needs the screen, and remember that the bridge started it."""
    if peekaboo_running():
        return
    log("starting Peekaboo")
    subprocess.run(["open", "-g", "-a", "Peekaboo"], check=True)
    with open(STARTED_PEEKABOO, "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
    for _ in range(40):
        time.sleep(0.5)
        if pk("window", "list", "--app", APP, check=False).get("success"):
            return
    raise RuntimeError("Peekaboo did not start (check its Accessibility and Screen Recording permissions).")


def quit_peekaboo(only_if_ours=True):
    """Quit Peekaboo.app when no run needs it. Checked first: telling an app that is not running to quit
    would launch it."""
    if only_if_ours and not os.path.exists(STARTED_PEEKABOO):
        return
    if peekaboo_running():
        subprocess.run(["osascript", "-e", 'tell application "Peekaboo" to quit'], capture_output=True)
    with contextlib.suppress(OSError):
        os.remove(STARTED_PEEKABOO)


@contextlib.contextmanager
def registered(args):
    """Register this run while it may use the screen, so `status` and `stop` know about it."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    path = os.path.join(RUNS_DIR, f"{os.getpid()}.json")
    what = getattr(args, "marker", None) or getattr(args, "title", None) or getattr(args, "conversation", None) \
        or (os.path.basename(args.attach) if getattr(args, "attach", None) else None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "command": args.cmd, "what": what, "cwd": os.getcwd(),
                   "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, ensure_ascii=False)
    try:
        start_peekaboo()
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
        if not active_runs(exclude=os.getpid()):
            quit_peekaboo()


def pk(*args, check=True, timeout=60, prepared=False):
    if args and args[0] in UI_ACTIONS:
        check_stop()
        if not prepared:
            wait_until_user_away()
        check_stop()
        if front_bundle() != BUNDLE:
            raise RuntimeError("ChatGPT lost foreground focus; refusing UI input.")
    cmd = ["peekaboo", *args, "--bridge-socket", BRIDGE, "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=UTF8)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"peekaboo {args[0]}: {(p.stdout or p.stderr).strip()[:500]}")
    if check and not d.get("success"):
        raise RuntimeError(f"peekaboo {' '.join(args[:2])}: {(d.get('error') or {}).get('message')}")
    return d


# ---------------------------------------------------------------- UI model

class UI:
    def __init__(self, data):
        self.snapshot = data["snapshot_id"]
        self.els = data["ui_elements"]

    def all(self, role=None, label=None, prefix=None):
        out = []
        for e in self.els:
            lab = e.get("label") or ""
            if role and e.get("ax_role") != role:
                continue
            if label is not None and lab != label:
                continue
            if prefix is not None and not lab.startswith(prefix):
                continue
            out.append(e)
        return out

    def one(self, **kw):
        found = self.all(**kw)
        return found[-1] if found else None

    @property
    def title(self):
        # The main conversation is the largest web area; side panels (tabs such as "Home") are smaller.
        areas = self.all(role="AXWebArea")
        if not areas:
            return ""
        main = max(areas, key=lambda a: a["bounds"]["width"] * a["bounds"]["height"])
        return main.get("label") or ""

    @property
    def window_top(self):
        win = self.one(role="AXWindow")
        return win["bounds"]["y"] if win else 0

    @property
    def input(self):
        return self.one(role="AXTextArea")

    @property
    def input_text(self):
        # An empty composer reports its placeholder as the value.
        value = (self.input or {}).get("value") or ""
        return "" if value == (self.input or {}).get("label") else value

    def composer_buttons(self):
        anchor = self.one(role="AXButton", label="Add files and more")
        if not anchor:
            return []
        a = anchor["bounds"]
        return [e for e in self.els if e.get("ax_role") in ("AXButton", "AXPopUpButton")
                and abs(e["bounds"]["y"] - a["y"]) < 15 and a["x"] - 10 <= e["bounds"]["x"] <= a["x"] + 760]

    @property
    def send_button(self):
        return next((e for e in self.composer_buttons() if e.get("label") == "Send"), None)

    @property
    def busy(self):
        return any("stop" in (e.get("label") or "").lower() for e in self.composer_buttons())

    @property
    def has_messages(self):
        return bool(self.all(role="AXButton", label="Copy message") or self.all(label="Regenerate response"))

    def answer_copy_button(self):
        """Copy button on the bottom-most assistant action bar (same row as Regenerate)."""
        # The action bar under an answer: Copy (shown as "Copied" for a while after a click), Share,
        # Regenerate response, Rate response, More actions. Any of the latter anchors the row.
        anchors = [e for e in self.els if e.get("label") in ("Regenerate response", "Rate response", "More actions")]
        if not anchors:
            return None
        ry = max(anchors, key=lambda e: e["bounds"]["y"])["bounds"]["y"]
        row = [c for c in self.els if c.get("ax_role") == "AXButton" and c.get("label") in ("Copy", "Copied")
               and abs(c["bounds"]["y"] - ry) < 12]
        return row[-1] if row else None


def main_window():
    wins = pk("window", "list", "--app", APP)["data"]["windows"]
    std = [w for w in wins if w.get("subrole") == "AXStandardWindow"] or wins
    if not std:
        raise RuntimeError("No ChatGPT window found. Open ChatGPT Desktop first.")
    return max(std, key=lambda w: w["bounds"]["width"] * w["bounds"]["height"])["window_id"]


_WINDOW = {}  # window id a caller holds -> ChatGPT's current window id (it recreates the window)


def observe(win):
    """Read ChatGPT's accessibility tree, healing the two ways a long run loses it:
    the window is recreated under a new id (reads then fail with "target attribution failed"), and
    after a relaunch Chromium exposes no web content until a web-focus read wakes it up."""
    for attempt in range(5):
        wid = _WINDOW.get(win, win)
        try:
            d = pk("see", "--app", APP, "--window-id", str(wid), "--tree", "--no-screenshot", *AX_LIMITS)
            ui = UI(d["data"])
            for element in ui.els:
                element["_window"] = win
            if ui.one(role="AXWebArea") or attempt == 4:
                return ui
            wake_web_content(wid)
        except (RuntimeError, subprocess.TimeoutExpired):
            if attempt == 4:
                raise
            try:
                _WINDOW[win] = main_window()
            except RuntimeError:
                pass
        time.sleep(2 * (attempt + 1))


def wake_web_content(wid):
    shot = os.path.join(STATE_DIR, "web-focus.png")
    os.makedirs(STATE_DIR, exist_ok=True)
    pk("see", "--app", APP, "--window-id", str(wid), "--web-focus", "--path", shot, *AX_LIMITS, check=False)
    if os.path.exists(shot):
        os.remove(shot)


def wait_for(win, pred, timeout, what, interval=0.5):
    end = time.time() + timeout
    while True:
        ui = observe(win)
        found = pred(ui)
        if found:
            return found
        if time.time() > end:
            raise RuntimeError(f"Timed out waiting for {what}.")
        time.sleep(interval)


# ---------------------------------------------------------------- input

def front_bundle():
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    p = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn], capture_output=True, text=True)
    return p.stdout.split("=", 1)[-1].strip().strip('"')


def previous_app():
    val = front_bundle()
    return val if val and val not in ("[ NULL ]", BUNDLE) else None


def restore(prev):
    if prev:
        subprocess.run(["open", "-b", prev])


def raise_chatgpt():
    check_stop()
    wait_until_user_away()
    for _ in range(10):
        subprocess.run(["open", "-b", BUNDLE])
        time.sleep(0.3)
        if front_bundle() == BUNDLE:
            return
    raise RuntimeError("Could not bring ChatGPT to the front; refusing to send input blind.")


def fresh_target(el, resolve=None):
    """Resolve after focus/idle waits; never reuse an old global coordinate."""
    win = el.get("_window")
    if win is None:
        raise RuntimeError("UI target has no observed window; refusing input.")
    ui = observe(win)
    if resolve:
        target = resolve(ui)
    else:
        matches = ui.all(role=el.get("ax_role"), label=el.get("label") or "")
        target = matches[0] if len(matches) == 1 else None
    if not target or not target.get("bounds") or target.get("is_enabled") is False:
        raise RuntimeError("UI target disappeared, is disabled, or is ambiguous; refusing input.")
    return target


def click(el, double=False, resolve=None):
    """Real foreground click using freshly resolved bounds."""
    raise_chatgpt()
    el = fresh_target(el, resolve)
    b = el["bounds"]
    x, y = round(b["x"] + b["width"] / 2), round(b["y"] + b["height"] / 2)
    pk("click", "--at", f"{x},{y}", "--global", "--foreground", *(["--double"] if double else []), prepared=True)


def hover(el):
    raise_chatgpt()
    el = fresh_target(el)
    b = el["bounds"]
    pk("move", "--at", f"{round(b['x'] + b['width'] / 2)},{round(b['y'] + b['height'] / 2)}", "--global",
       "--foreground", prepared=True)


def key(chord):
    raise_chatgpt()
    pk("press", chord, "--foreground", prepared=True)


def type_text(text):
    raise_chatgpt()
    pk("type", text, "--foreground", prepared=True)


def scroll_to_bottom(win, click_ok=False):
    """Bring the end of the answer (its action bar) on screen under the UI lock. Some layouts
    (e.g. with the agent side panel) ignore it, so with click_ok — only while holding the UI lock — fall
    back to clicking ChatGPT's own "Scroll to bottom" button."""
    for _ in range(3):
        raise_chatgpt()
        ui = observe(win)
        button = ui.one(role="AXButton", label="Scroll to bottom")
        if ui.answer_copy_button() is not None or not button:
            return ui
        bx = button["bounds"]["x"] + button["bounds"]["width"] / 2
        by = button["bounds"]["y"] + button["bounds"]["height"] / 2
        areas = [a for a in ui.all(role="AXScrollArea")
                 if a["bounds"]["x"] <= bx <= a["bounds"]["x"] + a["bounds"]["width"]
                 and a["bounds"]["y"] <= by <= a["bounds"]["y"] + a["bounds"]["height"]]
        if not areas:
            break
        area = min(areas, key=lambda a: a["bounds"]["width"] * a["bounds"]["height"])
        # Focus/idle waits precede this snapshot; dispatch without another stale-state wait.
        pk("scroll", "--direction", "down", "--amount", "80", "--on", area["id"], "--snapshot", ui.snapshot,
           check=False, prepared=True)
        time.sleep(0.4)
        ui = observe(win)
    button = ui.one(role="AXButton", label="Scroll to bottom")
    if click_ok and button and ui.answer_copy_button() is None:
        click(button)
        time.sleep(1.5)
        ui = observe(win)
    return ui


# ---------------------------------------------------------------- conversation steps

def open_new_chat(win):
    click(observe(win).one(role="AXButton", label="New chat") or _missing("New chat button"))
    # Never type until the window really shows an empty conversation, or the prompt would
    # land in whatever chat the user had open.
    wait_for(win, lambda ui: ui.title == NEW_CHAT_TITLE and not ui.has_messages and ui.input, 10,
             "an empty new chat")


def shows_marker(ui, marker):
    """Our message is on screen: the latest user message stays pinned at the top of the chat, with its
    attachment chip (a unique file name per run) and text. The sidebar is part of the same page and lists chat
    titles, which may contain the marker too (e.g. "kb-review A-007"), so it does not count."""
    if not marker:
        return False
    edge = max((r["bounds"]["x"] + r["bounds"]["width"] for r in conversation_rows(ui)), default=None)
    if edge is None:
        return False  # no reliable boundary between sidebar and conversation
    for e in ui.els:
        if marker in (e.get("label") or "") or marker in str(e.get("value") or ""):
            b = e.get("bounds")
            if not b or b["x"] < edge or e.get("ax_role") in ("AXTextArea", "AXWebArea", "AXWindow"):
                continue
            return True
    return False


_SEARCHES = 0


def conversation_area(ui):
    """The scroll area of the open conversation (the widest one right of the sidebar)."""
    edge = max((r["bounds"]["x"] + r["bounds"]["width"] for r in conversation_rows(ui)), default=0)
    areas = [a for a in ui.all(role="AXScrollArea") if a["bounds"]["x"] + a["bounds"]["width"] / 2 > edge]
    return max(areas, key=lambda a: a["bounds"]["width"] * a["bounds"]["height"]) if areas else None


def marker_in_chat(win, marker):
    """Look for the marker in the open chat: on screen, else scroll up to our message at the start of the
    conversation (the prompt names the marker in its first lines). Reads the screen only; copies nothing."""
    for _ in range(5):
        ui = observe(win)
        if shows_marker(ui, marker):
            return ui
        area = conversation_area(ui)
        if area is None:
            return None
        raise_chatgpt()
        ui = observe(win)
        area = conversation_area(ui)
        if area is None:
            return None
        pk("scroll", "--direction", "up", "--amount", "150", "--on", area["id"], "--snapshot", ui.snapshot,
           check=False, prepared=True)
        time.sleep(0.6)
    ui = observe(win)
    return ui if shows_marker(ui, marker) else None


def find_by_marker(win, marker, limit=6):
    """ChatGPT resets a chat's title when its first answer completes (overwriting a title we set while it was
    still generating), so a title can vanish from the sidebar. Open the most recent chats one by one and
    recognise ours by its marker; returns its current title or None. Other chats are only looked at, never
    copied, and a run searches at most MAX_SEARCHES times."""
    global _SEARCHES
    _SEARCHES += 1
    if _SEARCHES > MAX_SEARCHES:
        raise RuntimeError(f"Could not find the run's chat ({marker!r}) after {MAX_SEARCHES} searches; nothing was "
                           "copied. Open it in ChatGPT and run `wait` again.")
    ui = observe(win)
    recents = [e for e in ui.all(role="AXButton", label="Recents")]
    top = recents[0]["bounds"]["y"] if recents else ui.window_top + 50
    rows = sorted((e for e in conversation_rows(ui) if e["bounds"]["y"] > top), key=lambda e: e["bounds"]["y"])
    for row in rows[:limit]:
        click(row)
        time.sleep(3)  # the title switches before the messages do; a quick read still shows the old chat
        found = marker_in_chat(win, marker)
        if found is not None:
            time.sleep(1.5)
            again = observe(win)
            if again.title == found.title and shows_marker(again, marker):
                log(f"Found the run's chat by its marker: now titled {again.title!r}")
                scroll_to_bottom(win)
                return again.title
    log(f"none of the {min(len(rows), limit)} most recent chats is the run's ({marker!r}); "
        f"search {_SEARCHES} of {MAX_SEARCHES}")
    return None


def open_conversation(win, title, marker):
    if not marker:
        raise RuntimeError("Conversation identity is unknown; start a new chat.")
    ui = observe(win)
    if marker_in_chat(win, marker):
        return
    rows = [e for e in conversation_rows(ui) if e.get("label") == title]
    if len(rows) != 1:
        problem = "is not visible in the sidebar" if not rows else "matches several sidebar entries"
        raise RuntimeError(f"Conversation {title!r} {problem}; not sending.")
    click(rows[0])
    wait_for(win, lambda ui: ui.title == title and ui.input, 10, f"conversation {title!r} to open")
    if not marker_in_chat(win, marker):
        raise RuntimeError("Conversation marker did not match; refusing to upload or send.")


def validate_attachment_size(path):
    """Check on-disk upload bytes without reading/decompressing the attachment."""
    try:
        info = os.stat(path)
    except OSError as e:
        raise RuntimeError(f"Cannot inspect attachment {os.fspath(path)!r}: {e}") from e
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"Attachment is not a regular file: {os.fspath(path)!r}")
    if info.st_size > MAX_CHATGPT_UPLOAD_SIZE_BYTES:
        raise RuntimeError(
            f"Attachment upload size exceeds the limit: {os.fspath(path)!r} is "
            f"{info.st_size:,} bytes; maximum is 512 MiB "
            f"({MAX_CHATGPT_UPLOAD_SIZE_BYTES:,} bytes). Nothing was uploaded. "
            "For ZIP files this checks the compressed file size. Reduce or split the attachment; "
            "changing --budget or CHATGPT_BRIDGE_ATTACH_TOKENS does not change the upload limit."
        )
    return info.st_size


def require_target(ui, marker=None):
    require_chat_ui(ui)
    if marker:
        if not shows_marker(ui, marker):
            raise RuntimeError("Target conversation identity is not visible; refusing upload or input.")
    elif ui.title != NEW_CHAT_TITLE or ui.has_messages:
        raise RuntimeError("Expected an empty new chat; refusing upload or input.")
    if ui.busy or ui.input_text:
        raise RuntimeError("Target chat is generating or contains a draft; refusing upload or input.")


def attach(win, path, conversation_marker=None):
    validate_attachment_size(path)
    src = os.path.abspath(path)
    name = os.path.basename(src)
    # The open panel lists only what is on screen, so the file goes alone into a fresh folder.
    outbox = os.path.join(STATE_DIR, "outbox")
    if os.path.isdir(outbox):
        for old in os.listdir(outbox):
            path = os.path.join(outbox, old)
            if time.time() - os.path.getmtime(path) > 3600:
                shutil.rmtree(path, ignore_errors=True)
    folder = os.path.join(outbox, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    staged = os.path.join(folder, name)
    shutil.copy2(src, staged)
    # Recheck the actual upload copy in case the source grew after preflight.
    validate_attachment_size(staged)
    def upload_target(ui):
        require_target(ui, conversation_marker)
        return ui.one(role="AXButton", label="Add files and more")

    click(observe(win).one(role="AXButton", label="Add files and more") or _missing("attach button"),
          resolve=upload_target)
    item = wait_for(win, lambda ui: next(iter(ui.all(role="AXButton", prefix="Add photos & files")), None), 5,
                    "the upload menu item")
    click(item)

    def file_icon(ui):
        return next((e for e in ui.els if (e.get("label") or "").split(",")[0] == name), None)

    try:
        wait_for(win, lambda ui: ui.one(role="AXButton", label="Open") and ui.one(role="AXButton", label="Cancel"),
                 10, "the file dialog")
        key("cmd+shift+g")
        time.sleep(0.8)
        type_text(folder + "/")
        # The Go-to completion list loads lazily and swallows an early Return, so press until the folder opens.
        icon = None
        for _ in range(4):
            time.sleep(0.8)
            key("Return")
            try:
                icon = wait_for(win, file_icon, 2.5, f"{name} in the file dialog")
                break
            except RuntimeError:
                continue
        if not icon:
            raise RuntimeError(f"The file dialog never showed {name}.")
        click(icon, double=True)
        wait_for(win, lambda ui: ui.one(role="AXButton", label=f"Remove {name}"), 30, "the attachment card")
    except Exception:
        # Escape closes the Go-to field, a second one cancels the dialog (a Cancel click is
        # ignored while the Go-to field is open).
        for _ in range(2):
            ui = observe(win)
            if not (ui.one(role="AXButton", label="Open") and ui.one(role="AXButton", label="Cancel")):
                break
            key("Escape")
            time.sleep(0.6)
        raise
    log(f"Attached {name}")


def require_chat_ui(ui):
    switches = ui.all(prefix="Switch mode, current mode:")
    if len(switches) != 1:
        raise RuntimeError("Cannot confirm Chat mode; refusing to upload or send.")
    switch = switches[0]
    if not switch["label"].endswith(": ChatGPT"):
        raise RuntimeError(f"ChatGPT is not in Chat mode ({switch['label']!r}); switch it back to Chat first.")


def require_chat_mode(win):
    require_chat_ui(observe(win))


def set_prompt(win, prompt, conversation_marker=None):
    raise_chatgpt()
    ui = observe(win)
    require_target(ui, conversation_marker)
    box = ui.input or _missing("ChatGPT input box")
    if ui.input_text:
        raise RuntimeError(f"The input box already holds a draft ({ui.input_text[:60]!r}); refusing to overwrite it.")
    # set-value reports an untrusted receipt for Chromium but does dispatch; verify by re-reading.
    pk("set-value", "--on", box["id"], "--snapshot", ui.snapshot, "--value", prompt, check=False, prepared=True)
    time.sleep(0.4)
    ui = observe(win)
    if normalize_newlines(ui.input_text) != normalize_newlines(prompt):
        raise RuntimeError(f"Input box does not hold the full prompt (box shows {ui.input_text[:80]!r}...).")


def send(win, prompt, marker):
    # Uploads keep Send disabled until they finish.
    button = wait_for(win, lambda ui: ui.send_button if (ui.send_button or {}).get("is_enabled") else None,
                      300, "Send to become available")
    def target(ui):
        require_chat_ui(ui)
        if normalize_newlines(ui.input_text) != normalize_newlines(prompt):
            raise RuntimeError("Prompt changed before Send; delivery not attempted.")
        return ui.send_button

    log("Sending prompt...")
    click(button, resolve=target)
    wait_for(win, lambda ui: shows_marker(ui, marker) and (ui.busy or not ui.input_text), 10,
             "the sent request marker (delivery is unknown; do not resend automatically)")


def rename_conversation(win, new_title, marker):
    """Give the open chat a title of our own: ChatGPT keeps renaming auto-titled chats while and after
    answering, and auto titles repeat ("代码审查报告" twice), which makes a chat impossible to find again.
    The value is written into the title field directly (typing goes through the input method)."""
    ui = observe(win)
    head = [e for e in ui.all(role="AXButton", label=ui.title) if e["bounds"]["y"] < ui.window_top + 45]
    if not head:
        raise RuntimeError("Could not find the conversation title in the window header.")
    def title_target(current):
        if not shows_marker(current, marker):
            raise RuntimeError("Request identity changed before renaming; leaving title unchanged.")
        candidates = [e for e in current.all(role="AXButton", label=current.title)
                      if e["bounds"]["y"] < current.window_top + 45]
        return candidates[0] if len(candidates) == 1 else None

    click(head[0], resolve=title_target)
    for _ in range(10):
        time.sleep(0.4)
        ui = observe(win)
        field = ui.one(role="AXTextField", label="Chat title")
        if field:
            break
    else:
        raise RuntimeError("Clicking the title did not open the rename field.")
    # Paste, not set-value: a value written straight into the field only changes what is displayed, and
    # ChatGPT later restores its own title. Paste fires real input events (and, unlike typing, does not go
    # through the input method).
    raise_chatgpt()
    ui = observe(win)
    if not shows_marker(ui, marker) or not ui.one(role="AXTextField", label="Chat title"):
        raise RuntimeError("Rename field or request identity changed; refusing to type.")
    pk("press", "cmd+a", "--foreground", prepared=True)
    # Peekaboo cannot confirm a hotkey paste ("did not return a confirmed outcome") even when it worked:
    # judge by the resulting title instead.
    pk("paste", "--text", new_title, "--foreground", check=False, prepared=True)
    time.sleep(0.4)
    if observe(win).one(role="AXTextField", label="Chat title"):
        pk("press", "Return", "--foreground", prepared=True)
    wait_for(win, lambda u: u.title == new_title, 6, f"the chat to be titled {new_title!r}")
    time.sleep(3)
    if new_title not in sidebar_titles(observe(win)) and observe(win).title != new_title:
        raise RuntimeError(f"ChatGPT did not keep the title {new_title!r}")
    log(f"Conversation renamed to {new_title!r}")
    return new_title


def pin_title(win, prompt, marker):
    """Remember the new chat's title so a long run can be found again.

    Right after sending, the page is titled with the prompt itself; ChatGPT renames the chat a little
    later. Only a title that differs from the prompt, seen twice while our reply is generating, counts.
    """
    # ChatGPT shows the prompt with its Markdown stripped (no backticks, #, *), so compare letters only.
    def plain(text):
        return re.sub(r"[^\w]", "", str(text or ""))

    head = plain(prompt)[:20]

    def real_title(ui):
        t = plain(ui.title)
        if not shows_marker(ui, marker) or not ui.busy or ui.title in ("", NEW_CHAT_TITLE) or t.startswith(head) or head.startswith(t):
            return None
        return ui.title

    end = time.time() + 180
    last = None
    while time.time() < end:
        title = real_title(observe(win))
        if title and title == last:
            log(f"Conversation title: {title!r}")
            return title
        last = title
        time.sleep(4)
    log("warning: could not pin the conversation title; switching chats during the run may confuse the wait")
    return None


def conversation_rows(ui):
    """Sidebar rows that are conversations: each has its own "Chat actions" menu on the same row.

    Navigation buttons (Home, Plugins, …), New chat and project folders have none, so a page called
    "Home" can never be mistaken for a conversation.
    """
    menus = [m["bounds"] for m in ui.all(label="Chat actions")]

    def owns(row, m):  # the menu sits inside the row, at its right end
        r = row["bounds"]
        return (abs(r["y"] + r["height"] / 2 - (m["y"] + m["height"] / 2)) < 10
                and r["x"] <= m["x"] <= r["x"] + r["width"] and r["width"] > 3 * m["width"])

    return [e for e in ui.all(role="AXButton")
            if e.get("label") and e.get("label") != "Chat actions" and any(owns(e, m) for m in menus)]


def sidebar_titles(ui):
    return {e.get("label") for e in conversation_rows(ui)}


def row_positions(ui):
    return {e["label"]: (e["bounds"]["x"], e["bounds"]["y"]) for e in conversation_rows(ui)}


def renamed(ui, old_title, last_rows):
    """ChatGPT renames a chat in place: the sidebar row that showed the old title now shows the new one.

    Navigating elsewhere never changes a row's label, and the sidebar may be scrolled or partly hidden,
    so only that same-row evidence counts.
    """
    pos = (last_rows or {}).get(old_title)
    if not pos or ui.title in ("", NEW_CHAT_TITLE):
        return False
    return any(abs(x - pos[0]) < 6 and abs(y - pos[1]) < 6 and label == ui.title
               for label, (x, y) in row_positions(ui).items())


def wait_for_answer(win, timeout, poll, prev=None, title=None, marker=None):
    """Return once the reply is complete; returns the conversation title (ChatGPT may rename it)."""
    start = time.time()
    seen_busy = False
    stable = 0
    away = None
    maybe = None  # a new title seen once with our marker on screen
    while time.time() - start < timeout:
        time.sleep(poll)
        check_stop()
        ui = observe(win)
        # ChatGPT renames a chat while and after answering (seen: prompt text -> "X_request" -> "X发现").
        # Only the run's own marker proves a rename. (Guessing from sidebar positions was wrong: new chats
        # push older ones down, so another run's chat took over our row and its answer got copied.)
        # Right after a chat switch the new title shows above the old chat's messages, so a rename counts
        # only when the next read agrees.
        if title and ui.title != title and shows_marker(ui, marker):
            if maybe != ui.title:
                maybe = ui.title
                time.sleep(1.5)
                continue
            log(f"Conversation renamed {title!r} -> {ui.title!r}")
            title = ui.title
        maybe = None
        # Another chat is on screen (the user's, or another run's: chats generate in parallel). Go back to
        # ours once the window has been idle for two minutes, or after ten minutes in any case so parallel
        # runs take turns; never while a draft is open, and never in the middle of another run's step.
        if title and ui.title != title:
            if away is None:
                away = time.time()
                log(f"ChatGPT is showing {ui.title!r}; waiting before switching back to {title!r}")
            elif not ui.input_text and ((not ui.busy and time.time() - away > 120) or time.time() - away > 600):
                with ui_step("switching back"):
                    now = observe(win)
                    if now.title != title and not now.input_text:
                        try:
                            open_conversation(win, title, marker)
                        except RuntimeError as e:  # renamed out of sight, or scrolled out of the sidebar
                            found = find_by_marker(win, marker) if marker else None
                            if found:
                                title = found
                            else:
                                log(f"{e} Still waiting; open it in ChatGPT to let the run finish.")
                restore(prev)
                away = time.time()
            continue
        away = None
        # For tasks that read files ChatGPT may offer "Continue in ChatGPT Work" with a countdown;
        # the review must stay in Chat.
        stay = next(iter(ui.all(role="AXButton", prefix="Stay in Chat")), None)
        if stay:
            log("ChatGPT offered to switch to Work mode; choosing Stay in Chat")
            with ui_step("Stay in Chat"):
                def stay_target(current):
                    if not shows_marker(current, marker):
                        raise RuntimeError("Request identity changed before mode confirmation.")
                    return next(iter(current.all(role="AXButton", prefix="Stay in Chat")), None)
                click(stay, resolve=stay_target)
            restore(prev)
            continue
        if os.environ.get("CHATGPT_BRIDGE_DEBUG"):
            log(f"t={time.time() - start:.0f}s composer={[e.get('label') for e in ui.composer_buttons()]} "
                f"busy={ui.busy} copy={ui.answer_copy_button() is not None}")
        if ui.busy:
            if not seen_busy:
                log("ChatGPT is generating...")
            seen_busy, stable = True, 0
            continue
        if ui.answer_copy_button() is None and ui.one(role="AXButton", label="Scroll to bottom"):
            with ui_step("scrolling to the end of the answer"):
                if marker_in_chat(win, marker):
                    ui = scroll_to_bottom(win, click_ok=True)
        ready = ui.answer_copy_button() is not None and not ui.input_text
        if ready and (seen_busy or time.time() - start > 20):
            stable += 1
            if stable >= 2:
                return title
        else:
            stable = 0
    raise TimeoutError(f"No finished answer after {timeout}s (it may still be generating; use read-last later).")


def code_block_copy_button(ui):
    """The Copy button in the header of the answer's last code block (headers stay on screen while the block
    is in view), i.e. the bottom-most "Copy" above the answer's action bar."""
    bar = ui.answer_copy_button()
    if bar is None:
        return None
    above = [e for e in ui.els if e.get("ax_role") == "AXButton" and e.get("label") == "Copy"
             and e["bounds"]["y"] < bar["bounds"]["y"] - 20]
    return max(above, key=lambda e: e["bounds"]["y"]) if above else None


def _clicked_text(btn, resolve):
    """Click a copy button; the new clipboard text, or None if the clipboard did not change."""
    subprocess.run(["pbcopy"], input=SENTINEL, text=True, env=UTF8, check=True)
    click(btn, resolve=resolve)
    for _ in range(20):
        time.sleep(0.25)
        text = subprocess.run(["pbpaste"], capture_output=True, text=True, env=UTF8).stdout
        if text != SENTINEL:
            return text
    return None


def copy_answer(win, marker=None):  # caller holds the UI lock
    def target(ui, code_block=False):
        if marker and not shows_marker(ui, marker):
            raise RuntimeError("Request identity is not visible at copy time; refusing to copy another chat.")
        return code_block_copy_button(ui) if code_block else ui.answer_copy_button()

    ui = scroll_to_bottom(win, click_ok=True)
    btn = ui.answer_copy_button() or _missing("a Copy button for an answer")
    for _ in range(3):  # a recent click leaves it as "Copied" for a while; move off it and let it reset
        if btn.get("label") != "Copied":
            break
        if ui.input:
            hover(ui.input)
        time.sleep(2)
        ui = observe(win)
        btn = ui.answer_copy_button() or btn
    pk("clipboard", "save", "--slot", "chatgpt_bridge")
    try:
        for _ in range(2):
            text = _clicked_text(btn, target)
            if text is not None:
                return text
            ui = scroll_to_bottom(win, click_ok=True)
            btn = ui.answer_copy_button() or btn
        # Once an answer has been copied, ChatGPT can leave its Copy button as "Copied" for good, and clicking
        # it no longer copies. The answer's last code block (where structured answers put their result) still
        # has a working Copy button of its own.
        block = code_block_copy_button(ui)
        if block is not None:
            text = _clicked_text(block, lambda ui: target(ui, code_block=True))
            if text is not None:
                log("the answer's Copy button is stuck; copied its last code block instead")
                return text
        raise RuntimeError("Clicked Copy but the clipboard never changed (was the mouse or focus in use?).")
    finally:
        pk("clipboard", "restore", "--slot", "chatgpt_bridge", check=False)


def settled_title(win):
    """A new chat gets its generated title shortly after the first answer."""
    try:
        return wait_for(win, lambda ui: ui.title if ui.title not in ("", NEW_CHAT_TITLE) else None, 15,
                        "the conversation title")
    except RuntimeError:
        title = f"untitled-{time.strftime('%Y%m%d-%H%M%S')}"
        log(f"ChatGPT did not title the conversation; recording it as {title!r}")
        return title


def _raise(msg):
    raise RuntimeError(msg)


def _missing(what):
    raise RuntimeError(f"Could not find {what} in the ChatGPT window.")


def normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------- context ledger

def est_tokens(text):
    """Deliberately high estimate: ~1 token per CJK character, ~1 per 3.5 other characters."""
    wide = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return wide + (len(text) - wide) * 2 // 7 + 1


def attachment_tokens(path):
    if zipfile.is_zipfile(path):
        total = 0
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not info.is_dir():
                    try:
                        total += est_tokens(z.read(info).decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
        return total
    try:
        with open(path, encoding="utf-8") as f:
            return est_tokens(f.read())
    except (UnicodeDecodeError, OSError):
        return 0


def load_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_ledger(ledger):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER)


@contextlib.contextmanager
def ledger_lock():
    """Serialize the entire read/modify/write, independently of the UI lock."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "ledger.lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_request(args, phase, title=None):
    """Persist identity before sending, so uncertain delivery can be recovered without resending."""
    folder = os.path.join(STATE_DIR, "requests")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, args.marker + ".json")
    data = {"marker": args.marker, "phase": phase, "conversation": title or args.conversation,
            "out": args.out, "previous_marker": getattr(args, "previous_marker", None),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(path + ".tmp", "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)
    return path


# ---------------------------------------------------------------- commands

@contextlib.contextmanager
def ui_step(what):
    """Clicks, typing, uploads, switching chats and copying all use the one ChatGPT window, so only one run
    does them at a time. Waiting for a reply needs no lock: ChatGPT generates several chats in parallel."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "ui-step.lock"), "w") as handle:
        start, announced = time.time(), False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                check_stop()
                if not announced and time.time() - start > 5:
                    log(f"another run is using the ChatGPT window; {what} waits for it")
                    announced = True
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def wait_for_no_draft(win, quiet, timeout):
    """Never take the window from someone typing: if the composer holds a draft, wait until it has been gone
    for `quiet` seconds. A reply being generated elsewhere is no reason to wait (chats run in parallel)."""
    start = time.time()
    if not observe(win).input_text:
        return
    log(f"a draft is open in ChatGPT; waiting until it has been gone for {quiet:.0f}s")
    since = None
    while time.time() - start < timeout:
        time.sleep(10)
        if observe(win).input_text:
            since = None
        else:
            since = since or time.time()
            if time.time() - since >= quiet:
                return
    raise TimeoutError(f"A draft stayed open in ChatGPT for {timeout:.0f}s; nothing was sent.")


def cmd_stop(args):
    """Switch off, wait until every registered run has really ended, then quit Peekaboo."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STOP_FILE, "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
    print("bridge switched off; waiting for running runs to end...", flush=True)
    end = time.time() + args.wait
    while active_runs() and time.time() < end:
        time.sleep(1)
    left = active_runs()
    if left and args.force:
        for run in left:
            with contextlib.suppress(OSError):
                os.kill(run["pid"], signal.SIGTERM)
        time.sleep(3)
        left = active_runs()
    for run in left:
        print(f"still running: pid {run['pid']} {run['command']} {run.get('what') or ''} (since {run['started']}); "
              "`stop --force` ends it")
    if not left and not args.keep_peekaboo:
        quit_peekaboo(only_if_ours=False)
    print("stopped: no run is using the screen" + ("" if args.keep_peekaboo or left else "; Peekaboo quit")
          if not left else "not everything stopped (see above)")
    if left:
        sys.exit(1)


def cmd_resume(_args):
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    print("bridge switched on")


def cmd_status(_args):
    runs = active_runs()
    print(json.dumps({"switched_on": not os.path.exists(STOP_FILE), "peekaboo_running": peekaboo_running(),
                      "runs": runs}, ensure_ascii=False, indent=2))
    if os.path.exists(STOP_FILE) or not peekaboo_running():
        return  # never start Peekaboo just to look
    win = main_window()
    ui = observe(win)
    entry = load_ledger().get(ui.title)
    print(json.dumps({
        "window_id": win,
        "title": ui.title,
        "has_messages": ui.has_messages,
        "busy": ui.busy,
        "input_text": ui.input_text,
        "composer": [e.get("label") for e in ui.composer_buttons()],
        "answer_copy_button": bool(ui.answer_copy_button()),
        "ledger": entry,
    }, ensure_ascii=False, indent=2))


def cmd_usage(args):
    ledger = load_ledger()
    data = {args.title: ledger.get(args.title)} if args.title else ledger
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_read_last(args):
    prev = previous_app()
    win = main_window()
    try:
        with ui_step("copying"):
            if observe(win).busy:
                raise RuntimeError("ChatGPT is still generating; the visible Copy button would belong to an "
                                   "older answer.")
            text = copy_answer(win)
    finally:
        restore(prev)
    emit(text, args.out)


def cmd_ask(args):
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        raise RuntimeError("Empty prompt.")
    if args.attach:
        validate_attachment_size(args.attach)
    args.marker = "bridge-run-" + uuid.uuid4().hex
    prompt += ("\n\n[Bridge request identity]\n" + args.marker +
               "\nReturn this exact identity in your answer. If answering with JSON, "
               "include it in the summary string. Do not change the requested output format.")
    # Heuristic bookkeeping only: the cap does not measure or bound ChatGPT's file reads.
    cost = est_tokens(prompt) + (min(attachment_tokens(args.attach), ATTACH_CONTEXT) if args.attach else 0)
    ledger = load_ledger()
    args.previous_marker = None
    if args.conversation:
        entry = ledger.get(args.conversation)
        if entry is None:
            raise RuntimeError(f"No ledger entry for {args.conversation!r}; its usage is unknown. Use --new-chat.")
        if not entry.get("marker"):
            raise RuntimeError("This conversation has no verified request identity. Use --new-chat.")
        if entry.get("ambiguous"):
            raise RuntimeError("This title belongs to multiple conversations; use --new-chat with a handoff.")
        args.previous_marker = entry["marker"]
        if entry["tokens"] + cost > args.budget:
            raise BudgetExceeded(f"{args.conversation!r} has a local estimate of ~{entry['tokens']} tokens; "
                                 f"this turn adds ~{cost}, over the local {args.budget} token budget. "
                                 "This is separate from the attachment upload size limit. "
                                 "Start a new chat with a handoff summary.")
    elif cost > args.budget:
        log(f"warning: this turn's local estimate is ~{cost} tokens, above the {args.budget} token budget")

    receipt = write_request(args, "prepared")
    log(f"Request identity: {args.marker}; recovery receipt: {receipt}")
    win = main_window()
    while True:
        if args.wait_idle:
            wait_for_no_draft(win, args.idle_quiet, args.idle_timeout)
        elif observe(win).input_text:
            raise RuntimeError("A draft is open in ChatGPT; touching nothing.")
        with ui_step("sending"):
            if observe(win).input_text:  # someone started typing while this run waited for the window
                if not args.wait_idle:
                    raise RuntimeError("A draft is open in ChatGPT; touching nothing.")
                continue
            prev = previous_app()  # taken only now: the waits above can be long
            try:
                if args.new_chat:
                    open_new_chat(win)
                else:
                    open_conversation(win, args.conversation, entry["marker"])
                require_chat_mode(win)
                if observe(win).busy:
                    raise RuntimeError("Target chat is still generating; refusing to upload or send.")
                if args.attach:
                    attach(win, args.attach, entry["marker"] if args.conversation else None)
                require_chat_mode(win)
                set_prompt(win, prompt, entry["marker"] if args.conversation else None)
                write_request(args, "sending")
                try:
                    send(win, prompt, args.marker)
                except (RuntimeError, TimeoutError, subprocess.TimeoutExpired) as e:
                    write_request(args, "delivery_unknown")
                    raise RuntimeError(f"Delivery could not be confirmed; do not resend automatically. "
                                       f"Inspect {receipt} and recover with wait --marker {args.marker}. {e}") from e
                write_request(args, "sent")
                pinned = pin_title(win, prompt, args.marker) if args.new_chat else args.conversation
                if args.new_chat and args.title:
                    try:
                        pinned = rename_conversation(win, args.title, args.marker)
                    except RuntimeError as e:
                        log(f"warning: could not rename the chat ({e}); following ChatGPT's own title instead")
                write_request(args, "sent", pinned)
            finally:
                restore(prev)
        break
    try:
        text, title = collect_answer(win, args, prev, pinned, args.marker)
    finally:
        restore(prev)
    record(title, cost, text, args)
    write_request(args, "completed", title)


def collect_answer(win, args, prev, pinned, marker=None):
    if not marker:
        raise RuntimeError("No request identity; refusing to collect an unverified answer.")
    deadline = time.time() + args.timeout
    for _ in range(4):
        pinned = wait_for_answer(win, max(120, deadline - time.time()), args.poll, prev, pinned, marker)
        log("Answer finished, copying...")
        text = None
        with ui_step("copying"):
            ui = observe(win)
            if not marker_in_chat(win, marker):
                try:
                    open_conversation(win, pinned, marker)
                except RuntimeError:
                    pinned = find_by_marker(win, marker) or _raise(
                        f"Could not find the chat {pinned!r} again.")
            if observe(win).busy:  # the chat found again is still answering
                log("that chat is still generating; waiting for it")
                continue
            try:
                text = copy_answer(win, marker)
            except RuntimeError as e:
                log(f"{e} Looking again.")
            title = pinned or settled_title(win)
        if text is not None and marker in text:
            return text, title
        if text is not None:
            log(f"the copied answer does not contain {marker!r} (another chat was on screen); looking again")
        if marker:
            with ui_step("finding the chat"):
                found = find_by_marker(win, marker)
            if found:
                pinned = found
    raise RuntimeError(f"Could not copy an answer containing {marker!r}; nothing was written.")


def cmd_wait(args):
    """Attach to a reply that is already being generated (e.g. after the original run died)."""
    if not re.fullmatch(r"bridge-run-[0-9a-f]{32}", args.marker):
        raise RuntimeError("Use the exact bridge-run identity from the sending run's recovery receipt.")
    receipt_path = os.path.join(STATE_DIR, "requests", args.marker + ".json")
    try:
        with open(receipt_path, encoding="utf-8") as stream:
            receipt = json.load(stream)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Cannot read recovery receipt: {receipt_path}") from e
    args.previous_marker = receipt.get("previous_marker")
    cost = 0
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            cost += est_tokens(f.read())
    if args.attach:
        cost += min(attachment_tokens(args.attach), ATTACH_CONTEXT)
    prev = previous_app()
    win = main_window()
    try:
        text, title = collect_answer(win, args, prev, args.conversation, args.marker)
    finally:
        restore(prev)
    record(title, cost, text, args)
    write_request(args, "completed", title)


def record(title, cost, text, args):
    with ledger_lock():
        ledger = load_ledger()
        entry = ledger.setdefault(title, {"tokens": 0, "turns": 0, "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if entry.get("marker") and entry["marker"] not in (args.marker, getattr(args, "previous_marker", None)):
            # Titles are not unique IDs. Do not let a later continuation target a different chat.
            entry["ambiguous"] = True
        if entry.get("marker") != args.marker:
            entry["tokens"] += cost + est_tokens(text)
            entry["turns"] += 1
        entry["marker"] = args.marker
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_ledger(ledger)
    meta = {"title": title, "tokens_used": entry["tokens"], "turns": entry["turns"], "budget": args.budget,
            "remaining": args.budget - entry["tokens"], "marker": args.marker}
    log(f"Conversation {title!r}: ~{entry['tokens']}/{args.budget} tokens after {entry['turns']} turn(s)")
    emit(text, args.out)
    if args.out:
        with open(args.out + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


def emit(text, out):
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        log(f"Wrote {len(text)} chars to {out}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def main():
    # `kill` (SIGTERM) runs the cleanup too: the clipboard is restored, the run unregistered, Peekaboo quit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("stop", help="switch the bridge off: every run in every session stops touching the screen; "
                                    "waits for them to end, then quits Peekaboo")
    p.add_argument("--wait", type=float, default=90, help="seconds to wait for running runs to end")
    p.add_argument("--force", action="store_true", help="terminate runs still alive after --wait")
    p.add_argument("--keep-peekaboo", action="store_true", help="leave Peekaboo.app running")
    p.set_defaults(fn=cmd_stop)
    sub.add_parser("resume", help="switch the bridge back on").set_defaults(fn=cmd_resume)
    p = sub.add_parser("usage")
    p.add_argument("title", nargs="?")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("read-last")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_read_last)
    p = sub.add_parser("ask")
    p.add_argument("prompt", help='prompt text, or "-" to read stdin')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--new-chat", action="store_true", help="start a fresh conversation")
    target.add_argument("--conversation", help="continue the conversation with this exact title")
    p.add_argument("--title", help="with --new-chat: rename the chat to this unique title right after sending, "
                                   "so it can be found again later")
    p.add_argument("--attach", help="file to upload (e.g. a .zip); at most 512 MiB on disk, "
                   "account limits may be lower; independent of the token budget")
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the answer")
    p.add_argument("--poll", type=float, default=3, help="seconds between UI checks")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help="local estimated token budget per conversation, not an upload size limit")
    p.add_argument("--out", help="write the answer here (and metadata to <out>.meta.json)")
    p.add_argument("--no-wait-idle", dest="wait_idle", action="store_false",
                   help="fail at once if a draft is open in ChatGPT instead of waiting for it to go")
    p.add_argument("--idle-quiet", type=float, default=60,
                   help="when a draft was open, seconds it must stay gone before this run takes the window")
    p.add_argument("--idle-timeout", type=float, default=10800, help="give up waiting after this")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("wait", help="wait for a reply already in progress in a conversation, then copy it")
    p.add_argument("--marker", required=True, help="exact bridge-run identity from the sending run's receipt")
    p.add_argument("--conversation", required=True, help="exact title of the conversation")
    p.add_argument("--prompt-file", help="the prompt that was sent (for the token ledger)")
    p.add_argument("--attach", help="the file that was attached (for the token ledger)")
    p.add_argument("--timeout", type=float, default=10800)
    p.add_argument("--poll", type=float, default=10)
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_wait)
    args = ap.parse_args()
    try:
        if args.cmd not in ("stop", "resume", "usage", "status"):
            check_stop()  # switched off: do not even read the screen (that would start Peekaboo again)
        if args.cmd == "ask" and args.attach:
            validate_attachment_size(args.attach)  # before registered() starts Peekaboo
        if args.cmd in SCREEN_COMMANDS:
            with registered(args):
                args.fn(args)
        else:
            args.fn(args)
    except BudgetExceeded as e:
        print(f"budget: {e}", file=sys.stderr)
        sys.exit(3)
    except Stopped as e:
        print(f"stopped: {e}", file=sys.stderr)
        sys.exit(5)
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
