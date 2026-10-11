#!/usr/bin/env python3
"""Drive the ChatGPT desktop app through the Peekaboo CLI.

    chatgpt_bridge.py status
    chatgpt_bridge.py ask PROMPT|- (--new-chat [--title T] | --conversation TITLE) [--attach FILE]
                      [--timeout 1800] [--budget 90000] [--out answer.md]
    chatgpt_bridge.py read-last [--out answer.md]
    chatgpt_bridge.py wait --conversation TITLE [--out answer.md]   # take over a reply in progress
    chatgpt_bridge.py usage [TITLE]

Exit codes: 0 ok, 1 error, 3 the conversation's context budget would be exceeded
(start a new chat and hand the relevant context over in the prompt).

Every conversation gets a ledger entry (~/.chatgpt-bridge/ledger.json) with a deliberately
high token estimate of everything sent and received, so follow-ups can stop before the
chat's context window fills up. With --out, a <out>.meta.json records the title and usage.

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
import shutil
import subprocess
import sys
import time
import zipfile

BRIDGE = os.path.expanduser("~/Library/Application Support/Peekaboo/bridge.sock")
APP = "ChatGPT"
BUNDLE = "com.openai.codex"
AX_LIMITS = ["--depth", "80", "--max-children", "1000", "--max-elements", "20000"]
UTF8 = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
SENTINEL = "__chatgpt_bridge_sentinel__"
STATE_DIR = os.path.expanduser("~/.chatgpt-bridge")
LEDGER = os.path.join(STATE_DIR, "ledger.json")
NEW_CHAT_TITLE = "ChatGPT"
# Thinking mode is documented at 128K input tokens; stay well below it because hidden
# reasoning, tool output and file reads also take space.
DEFAULT_BUDGET = int(os.environ.get("CHATGPT_BRIDGE_BUDGET", "90000"))
ATTACH_CONTEXT = int(os.environ.get("CHATGPT_BRIDGE_ATTACH_TOKENS", "30000"))


class BudgetExceeded(Exception):
    pass


def log(msg):
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


def pk(*args, check=True, timeout=60):
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
        value = ((self.input or {}).get("value") or "").strip()
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
        regen = self.all(label="Regenerate response")
        if not regen:
            return None
        ry = max(regen, key=lambda e: e["bounds"]["y"])["bounds"]["y"]
        row = [c for c in self.all(role="AXButton", label="Copy") if abs(c["bounds"]["y"] - ry) < 12]
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
    for _ in range(10):
        subprocess.run(["open", "-b", BUNDLE])
        time.sleep(0.3)
        if front_bundle() == BUNDLE:
            return
    raise RuntimeError("Could not bring ChatGPT to the front; refusing to send input blind.")


def click(el, double=False):
    """Real foreground click at the element's centre (global screen points)."""
    raise_chatgpt()
    b = el["bounds"]
    x, y = round(b["x"] + b["width"] / 2), round(b["y"] + b["height"] / 2)
    pk("click", "--at", f"{x},{y}", "--global", "--foreground", *(["--double"] if double else []))


def hover(el):
    raise_chatgpt()
    b = el["bounds"]
    pk("move", "--at", f"{round(b['x'] + b['width'] / 2)},{round(b['y'] + b['height'] / 2)}", "--global",
       "--foreground")


def key(chord):
    raise_chatgpt()
    pk("press", chord, "--foreground")


def type_text(text):
    raise_chatgpt()
    pk("type", text, "--foreground")


def scroll_to_bottom(win):
    ui = observe(win)
    # A review answer can be very long (a 20-minute run produced tens of KB), so allow many large steps.
    for _ in range(40):
        button = ui.one(role="AXButton", label="Scroll to bottom")
        if not button:
            return ui
        # Scroll the area that holds the chat: the smallest one containing the "Scroll to bottom" button.
        bx = button["bounds"]["x"] + button["bounds"]["width"] / 2
        by = button["bounds"]["y"] + button["bounds"]["height"] / 2
        areas = [a for a in ui.all(role="AXScrollArea")
                 if a["bounds"]["x"] <= bx <= a["bounds"]["x"] + a["bounds"]["width"]
                 and a["bounds"]["y"] <= by <= a["bounds"]["y"] + a["bounds"]["height"]]
        if not areas:
            break
        area = min(areas, key=lambda a: a["bounds"]["width"] * a["bounds"]["height"])
        pk("scroll", "--direction", "down", "--amount", "80", "--on", area["id"], "--snapshot", ui.snapshot,
           check=False)
        time.sleep(0.4)
        ui = observe(win)
    return ui


# ---------------------------------------------------------------- conversation steps

def open_new_chat(win):
    click(observe(win).one(role="AXButton", label="New chat") or _missing("New chat button"))
    # Never type until the window really shows an empty conversation, or the prompt would
    # land in whatever chat the user had open.
    wait_for(win, lambda ui: ui.title == NEW_CHAT_TITLE and not ui.has_messages and ui.input, 10,
             "an empty new chat")


def open_conversation(win, title):
    ui = observe(win)
    if ui.title == title:
        return
    rows = [e for e in conversation_rows(ui) if e.get("label") == title]
    if len(rows) != 1:
        problem = "is not visible in the sidebar" if not rows else "matches several sidebar entries"
        raise RuntimeError(f"Conversation {title!r} {problem}; not sending.")
    click(rows[0])
    wait_for(win, lambda ui: ui.title == title and ui.input, 10, f"conversation {title!r} to open")


def attach(win, path):
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
    shutil.copy2(src, os.path.join(folder, name))
    click(observe(win).one(role="AXButton", label="Add files and more") or _missing("attach button"))
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


def require_chat_mode(win):
    switch = next(iter(observe(win).all(prefix="Switch mode, current mode:")), None)
    if switch and not switch["label"].endswith(": ChatGPT"):
        raise RuntimeError(f"ChatGPT is not in Chat mode ({switch['label']!r}); switch it back to Chat first.")


def set_prompt(win, prompt):
    ui = observe(win)
    box = ui.input or _missing("ChatGPT input box")
    if ui.input_text:
        raise RuntimeError(f"The input box already holds a draft ({ui.input_text[:60]!r}); refusing to overwrite it.")
    # set-value reports an untrusted receipt for Chromium but does dispatch; verify by re-reading.
    pk("set-value", "--on", box["id"], "--snapshot", ui.snapshot, "--value", prompt, check=False)
    time.sleep(0.4)
    ui = observe(win)
    if squash(ui.input_text) != squash(prompt):
        raise RuntimeError(f"Input box does not hold the full prompt (box shows {ui.input_text[:80]!r}...).")


def send(win):
    # Uploads keep Send disabled until they finish.
    button = wait_for(win, lambda ui: ui.send_button if (ui.send_button or {}).get("is_enabled") else None,
                      300, "Send to become available")
    log("Sending prompt...")
    click(button)
    wait_for(win, lambda ui: ui.busy or not ui.input_text, 5,
             "the prompt to leave the input box (it was not sent)")


def rename_conversation(win, new_title):
    """Give the open chat a title of our own: ChatGPT keeps renaming auto-titled chats while and after
    answering, and auto titles repeat ("代码审查报告" twice), which makes a chat impossible to find again.
    The value is written into the title field directly (typing goes through the input method)."""
    ui = observe(win)
    head = [e for e in ui.all(role="AXButton", label=ui.title) if e["bounds"]["y"] < ui.window_top + 45]
    if not head:
        raise RuntimeError("Could not find the conversation title in the window header.")
    click(head[0])
    for _ in range(10):
        time.sleep(0.4)
        ui = observe(win)
        field = ui.one(role="AXTextField", label="Chat title")
        if field:
            break
    else:
        raise RuntimeError("Clicking the title did not open the rename field.")
    pk("set-value", "--on", field["id"], "--snapshot", ui.snapshot, "--value", new_title, check=False)
    time.sleep(0.3)
    key("Return")
    wait_for(win, lambda u: u.title == new_title, 6, f"the chat to be titled {new_title!r}")
    log(f"Conversation renamed to {new_title!r}")
    return new_title


def pin_title(win, prompt):
    """Remember the new chat's title so a long run can be found again.

    Right after sending, the page is titled with the prompt itself; ChatGPT renames the chat a little
    later. Only a title that differs from the prompt, seen twice while our reply is generating, counts.
    """
    head = squash(prompt)[:20]

    def real_title(ui):
        t = squash(ui.title)
        if not ui.busy or ui.title in ("", NEW_CHAT_TITLE) or t.startswith(head) or head.startswith(t):
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


def wait_for_answer(win, timeout, poll, prev=None, title=None):
    """Return once the reply is complete; returns the conversation title (ChatGPT may rename it)."""
    start = time.time()
    seen_busy = False
    stable = 0
    away = None
    last_rows = None
    while time.time() - start < timeout:
        time.sleep(poll)
        ui = observe(win)
        # ChatGPT renames a chat while and after answering (seen: prompt text -> "X_request" -> "X发现").
        if title and ui.title != title and renamed(ui, title, last_rows):
            log(f"Conversation renamed {title!r} -> {ui.title!r}")
            title = ui.title
        last_rows = row_positions(ui)
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
                            open_conversation(win, title)
                        except RuntimeError as e:  # e.g. scrolled out of the sidebar; never click blind
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
                click(stay)
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
            ui = scroll_to_bottom(win)
        ready = ui.answer_copy_button() is not None and not ui.input_text
        if ready and (seen_busy or time.time() - start > 20):
            stable += 1
            if stable >= 2:
                return title
        else:
            stable = 0
    raise TimeoutError(f"No finished answer after {timeout}s (it may still be generating; use read-last later).")


def copy_answer(win):
    ui = scroll_to_bottom(win)
    btn = ui.answer_copy_button() or _missing("a Copy button for an answer")
    pk("clipboard", "save", "--slot", "chatgpt_bridge")
    try:
        subprocess.run(["pbcopy"], input=SENTINEL, text=True, env=UTF8, check=True)
        click(btn)
        for _ in range(20):
            time.sleep(0.25)
            text = subprocess.run(["pbpaste"], capture_output=True, text=True, env=UTF8).stdout
            if text != SENTINEL:
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


def _missing(what):
    raise RuntimeError(f"Could not find {what} in the ChatGPT window.")


def squash(text):
    return "".join(text.split())


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


def cmd_status(_args):
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
    if args.attach and not os.path.isfile(args.attach):
        raise RuntimeError(f"Attachment not found: {args.attach}")
    # ChatGPT works through an uploaded archive with its own tools instead of loading it whole, so an
    # attachment is charged as its text size capped at ATTACH_CONTEXT (what such a turn typically reads).
    cost = est_tokens(prompt) + (min(attachment_tokens(args.attach), ATTACH_CONTEXT) if args.attach else 0)
    ledger = load_ledger()
    if args.conversation:
        entry = ledger.get(args.conversation)
        if entry is None:
            raise RuntimeError(f"No ledger entry for {args.conversation!r}; its usage is unknown. Use --new-chat.")
        if entry["tokens"] + cost > args.budget:
            raise BudgetExceeded(f"{args.conversation!r} has used ~{entry['tokens']} tokens; this turn adds ~{cost}, "
                                 f"over the {args.budget} budget. Start a new chat with a handoff summary.")
    elif cost > args.budget:
        log(f"warning: this prompt alone is ~{cost} tokens, above the {args.budget} budget")

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
                    open_conversation(win, args.conversation)
                if args.attach:
                    attach(win, args.attach)
                require_chat_mode(win)
                set_prompt(win, prompt)
                send(win)
                pinned = pin_title(win, prompt) if args.new_chat else args.conversation
                if args.new_chat and args.title:
                    try:
                        pinned = rename_conversation(win, args.title)
                    except RuntimeError as e:
                        log(f"warning: could not rename the chat ({e}); following ChatGPT's own title instead")
            finally:
                restore(prev)
        break
    try:
        text, title = collect_answer(win, args, prev, pinned)
    finally:
        restore(prev)
    record(title, cost, text, args)


def collect_answer(win, args, prev, pinned):
    pinned = wait_for_answer(win, args.timeout, args.poll, prev, pinned)
    log("Answer finished, copying...")
    with ui_step("copying"):
        ui = observe(win)
        if pinned and ui.title != pinned:  # another run switched the window meanwhile
            open_conversation(win, pinned)
        text = copy_answer(win)
        title = pinned or settled_title(win)
    return text, title


def cmd_wait(args):
    """Attach to a reply that is already being generated (e.g. after the original run died)."""
    cost = 0
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            cost += est_tokens(f.read())
    if args.attach:
        cost += min(attachment_tokens(args.attach), ATTACH_CONTEXT)
    prev = previous_app()
    win = main_window()
    try:
        text, title = collect_answer(win, args, prev, args.conversation)
    finally:
        restore(prev)
    record(title, cost, text, args)


def record(title, cost, text, args):
    ledger = load_ledger()  # re-read: other runs may have written it while this one waited
    entry = ledger.setdefault(title, {"tokens": 0, "turns": 0, "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
    entry["tokens"] += cost + est_tokens(text)
    entry["turns"] += 1
    entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_ledger(ledger)
    meta = {"title": title, "tokens_used": entry["tokens"], "turns": entry["turns"], "budget": args.budget,
            "remaining": args.budget - entry["tokens"]}
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
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
    p.add_argument("--attach", help="file to upload with the prompt (e.g. a .zip)")
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the answer")
    p.add_argument("--poll", type=float, default=3, help="seconds between UI checks")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="token budget per conversation")
    p.add_argument("--out", help="write the answer here (and metadata to <out>.meta.json)")
    p.add_argument("--no-wait-idle", dest="wait_idle", action="store_false",
                   help="fail at once if a draft is open in ChatGPT instead of waiting for it to go")
    p.add_argument("--idle-quiet", type=float, default=60,
                   help="when a draft was open, seconds it must stay gone before this run takes the window")
    p.add_argument("--idle-timeout", type=float, default=10800, help="give up waiting after this")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("wait", help="wait for a reply already in progress in a conversation, then copy it")
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
        args.fn(args)
    except BudgetExceeded as e:
        print(f"budget: {e}", file=sys.stderr)
        sys.exit(3)
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
