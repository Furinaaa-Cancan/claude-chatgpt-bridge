#!/usr/bin/env python3
"""State, packaging, prompts and HTML reports for the ChatGPT code-review loop.

By default the whole code base goes to ChatGPT as one package: Pro unpacks it in its sandbox and works
through it with its own tools for as long as it needs, which also catches cross-module problems.
--chunk-tokens N optionally splits it into parts reviewed in separate conversations (a fallback when
ChatGPT reports large unread areas). Part numbers stay stable across rounds; later rounds only re-send
parts whose files changed or that have fixes or disputes waiting for ChatGPT's verdict.

State lives outside the project, in ~/.chatgpt-review/<name>-<hash>/ (state.json, round-N/part-K/).

    review.py init --root DIR [--name N] [--context T] [--focus T] [--brief FILE]
                   [--source-zip ZIP] [--diff FILE] [--include PATH ...] [--force]
    review.py pack [--root DIR] [--chunk-tokens 0] [--include-locks] [--max-file-kb 512]
    review.py next [--root DIR]                 # the next part to send: zip, prompt, out, conversation
    review.py prompt [--root DIR] [--part K] [--handoff]
    review.py ingest ANSWER.md [--root DIR] [--part K]
    review.py set ID [ID ...] [--verdict confirmed|disputed|uncertain] [--status open|fixed|verified|wontfix|deferred] [--note T]
    review.py report [--root DIR] [--summary T]
    review.py status [--root DIR]
"""
import argparse
import datetime as dt
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import string
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_STATE = os.path.expanduser("~/.chatgpt-review")
SEVERITIES = ["critical", "high", "medium", "low"]
VERDICTS = ["confirmed", "disputed", "uncertain"]
STATUSES = ["open", "fixed", "verified", "wontfix", "deferred"]
DEFAULT_FOCUS = ("正确性 bug、安全问题、数据丢失与并发问题、错误处理缺失、明显的性能瓶颈，"
                 "以及会导致 bug 的可维护性问题。")

SKIP_DIRS = {".git", ".hg", ".svn", ".chatgpt-review", "node_modules", "__pycache__", ".venv", "venv", "env",
             ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "out", ".next", ".nuxt",
             ".svelte-kit", "target", ".gradle", ".idea", ".vscode", "coverage", "htmlcov", "Pods",
             "DerivedData", ".terraform", ".cache", ".turbo", ".parcel-cache", "vendor", "__MACOSX"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
             "composer.lock", "Gemfile.lock", "go.sum", "uv.lock", "bun.lockb"}
SECRET_NAMES = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa*",
                "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", "credentials",
                "credentials*.json", "secret*.json", "secrets.y*ml", "secrets.toml", "*.secret",
                "service-account*.json", "*.mobileprovision", ".htpasswd"]
SECRET_EXAMPLE_OK = (".example", ".sample", ".template", ".dist")
SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}"
    r"|\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_\-]{35}\b")

STATUS_ZH = {"open": "未处理", "fixed": "已修复（待核实）", "verified": "已确认修复", "wontfix": "不修改",
             "deferred": "暂缓"}
VERDICT_ZH = {"confirmed": "核实成立", "disputed": "不成立", "uncertain": "存疑", None: "未核实"}


def est_tokens(text):
    """Deliberately high estimate (same as the bridge): ~1 token per CJK char, ~1 per 3.5 others."""
    wide = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return wide + (len(text) - wide) * 2 // 7 + 1


# ---------------------------------------------------------------- state

def state_dir(root):
    root = os.path.abspath(root)
    tag = hashlib.sha1(root.encode()).hexdigest()[:8]
    return os.path.join(HOME_STATE, f"{os.path.basename(root.rstrip('/')) or 'root'}-{tag}")


def load(root):
    path = os.path.join(state_dir(root), "state.json")
    if not os.path.exists(path):
        sys.exit(f"error: no review state for {root}; run `review.py init --root {root}` first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(state):
    d = state_dir(state["root"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, "state.json"))


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def current_round(state):
    return state["rounds"][-1] if state["rounds"] else None


def part_dir(state, rnd, k):
    d = os.path.join(state_dir(state["root"]), f"round-{rnd['n']}", f"part-{k}")
    os.makedirs(d, exist_ok=True)
    return d


def get_part(rnd, k):
    for p in rnd["parts"]:
        if p["k"] == k:
            return p
    sys.exit(f"error: part {k} is not in round {rnd['n']}")


def next_id(state):
    nums = [int(m.group(1)) for f in state["findings"] if (m := re.fullmatch(r"F(\d+)", f["id"]))]
    return f"F{max(nums, default=0) + 1}"


def find(state, fid):
    for f in state["findings"]:
        if f["id"] == fid:
            return f
    sys.exit(f"error: no finding {fid}")


# ---------------------------------------------------------------- collecting files

def source_root(state):
    return state.get("source") or state["root"]


def list_files(root, use_git):
    if use_git:
        try:
            out = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                                 capture_output=True, check=True).stdout
            return sorted(p for p in out.decode("utf-8", "replace").split("\0") if p)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(files)


def skip_reason(rel, data, include_locks, max_kb):
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return "excluded directory"
    name = parts[-1]
    lower = name.lower()
    if name == ".DS_Store" or name.startswith("._"):
        return "junk"
    if any(fnmatch.fnmatch(lower, pat) for pat in SECRET_NAMES) and not lower.endswith(SECRET_EXAMPLE_OK):
        return "possible secret (file name)"
    if name in LOCKFILES and not include_locks:
        return "lockfile"
    if len(data) > max_kb * 1024:
        return f"too large ({len(data) // 1024} KB)"
    if b"\0" in data[:8192]:
        return "binary"
    if SECRET_CONTENT.search(data.decode("utf-8", "replace")):
        return "possible secret (content)"
    return None


def included(rel, patterns):
    """A pattern is a directory, a file, or a glob, relative to the project root."""
    for pat in patterns:
        pat = pat.strip("/")
        if rel == pat or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat):
            return True
    return False


def collect(state, include_locks, max_kb):
    root = source_root(state)
    special = {state.get("brief_rel"), state.get("diff_rel")}
    include = state.get("include") or []
    files, skipped = [], []
    for rel in list_files(root, use_git=not state.get("source")):
        if rel in special or (include and not included(rel, include)):
            continue
        path = os.path.join(root, rel)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        reason = skip_reason(rel, data, include_locks, max_kb)
        if reason:
            skipped.append({"path": rel, "reason": reason})
            continue
        text = data.decode("utf-8", "replace")
        files.append({"path": rel, "lines": text.count("\n") + 1, "tokens": est_tokens(text),
                      "sha": hashlib.sha1(data).hexdigest()})
    return files, skipped


def assign_parts(state, files, limit):
    """Stable file -> part mapping; new files are packed greedily (in path order) into new parts."""
    part_of = state.setdefault("partition", {})
    k = max(part_of.values(), default=0)
    cur, cur_tokens = None, 0
    for f in files:
        if f["path"] in part_of:
            continue
        cost = f["tokens"] + f.get("diff_tokens", 0)
        if cur is None or (limit and cur_tokens and cur_tokens + cost > limit):
            k += 1
            cur, cur_tokens = k, 0
        part_of[f["path"]] = cur
        cur_tokens += cost


def pending_findings(state, k):
    return [f for f in state["findings"] if f.get("part") == k and (
        f["status"] == "fixed" or (f["status"] in ("wontfix", "deferred") and not f.get("chatgpt_reply")))]


def unquote_git_path(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
    return p


def split_diff(text):
    sections = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        m = re.match(r'diff --git (?:"?a/.*?"?) ("?b/.+?"?)\n', chunk)
        if m:
            path = unquote_git_path(m.group(1))
            path = path[2:] if path.startswith("b/") else path
            sections[path] = chunk
    return sections


# ---------------------------------------------------------------- pack

def cmd_pack(args):
    state = load(args.root)
    rnd = current_round(state)
    if rnd is None or all(p.get("answer") for p in rnd["parts"]):
        rnd = {"n": len(state["rounds"]) + 1, "started": now(), "parts": []}
        state["rounds"].append(rnd)
    files, skipped = collect(state, args.include_locks, args.max_file_kb)
    if not files:
        sys.exit("error: nothing to pack")
    diff_sections = {}
    if state.get("diff"):
        with open(state["diff"], encoding="utf-8", errors="replace") as f:
            diff_sections = split_diff(f.read())
    for f in files:
        f["diff_tokens"] = est_tokens(diff_sections[f["path"]]) if f["path"] in diff_sections else 0
    assign_parts(state, files, args.chunk_tokens)
    part_of = state["partition"]
    meta = state.setdefault("parts", {})
    by_part = {}
    for f in files:
        by_part.setdefault(part_of[f["path"]], []).append(f)
    total_parts = max(by_part)

    active, unchanged = [], []
    for k in sorted(by_part):
        m = meta.get(str(k))
        changed = (not m or any(m["hashes"].get(f["path"]) != f["sha"] for f in by_part[k])
                   or set(m["hashes"]) - {f["path"] for f in by_part[k]})
        (active if changed or pending_findings(state, k) else unchanged).append(k)
    if not active:
        if not rnd["parts"]:
            state["rounds"].pop()
        save(state)
        print(json.dumps({"round": None, "message": "nothing changed and nothing pending; no part needs review"},
                         ensure_ascii=False))
        return

    all_paths = [f["path"] for f in files]
    tree = "\n".join(f"- {p}" for p in all_paths) if len(all_paths) <= 1500 else \
        "\n".join(f"- {d}/ ({n} files)" for d, n in sorted(dir_counts(all_paths).items()))

    rnd["parts"] = []
    for k in active:
        fs = by_part[k]
        d = part_dir(state, rnd, k)
        for old in os.listdir(d):
            if old.endswith(".zip"):
                os.remove(os.path.join(d, old))
        suffix = f"round{rnd['n']}" if total_parts == 1 else f"r{rnd['n']}-part{k}of{total_parts}"
        zip_name = f"{state['name']}-{suffix}.zip"
        part_diff = "".join(diff_sections[f["path"]] for f in fs if f["path"] in diff_sections)
        lines = sum(f["lines"] for f in fs)
        manifest = [f"# {state['name']} — 第 {rnd['n']} 轮审查" + (f"，第 {k}/{total_parts} 部分" if total_parts > 1 else ""),
                    "", f"本包 {len(fs)} 个文件，约 {lines} 行。", "", "## 本包文件", "", "| 文件 | 行数 |", "|---|---|"]
        manifest += [f"| {f['path']} | {f['lines']} |" for f in fs]
        if part_diff:
            manifest += ["", f"`CHANGES.diff` 是本包文件相对上次审查的改动（{part_diff.count('diff --git')} 个文件）。"]
        if total_parts > 1:
            manifest += ["", "## 全项目文件（不在本包内的文件由其他对话审查，此处仅供了解结构）", "", tree]
        zip_path = os.path.join(d, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{state['name']}/REVIEW_MANIFEST.md", "\n".join(manifest) + "\n")
            if part_diff:
                z.writestr(f"{state['name']}/CHANGES.diff", part_diff)
            for f in fs:
                z.write(os.path.join(source_root(state), f["path"]), f"{state['name']}/{f['path']}")
        rnd["parts"].append({"k": k, "zip": zip_path, "files": [f["path"] for f in fs],
                             "hashes": {f["path"]: f["sha"] for f in fs}, "file_count": len(fs), "lines": lines,
                             "tokens": sum(f["tokens"] for f in fs) + est_tokens(part_diff),
                             "has_diff": bool(part_diff)})
    rnd["total_parts"] = total_parts
    save(state)
    secrets = [s["path"] for s in skipped if s["reason"].startswith("possible secret")]
    oversized = [f["path"] for f in files if args.chunk_tokens and f["tokens"] + f["diff_tokens"] > args.chunk_tokens]
    print(json.dumps({
        "round": rnd["n"], "total_parts": total_parts, "parts_to_send": len(rnd["parts"]),
        "unchanged_parts_skipped": unchanged, "files": len(files),
        "est_tokens": sum(p["tokens"] for p in rnd["parts"]),
        "parts": [{"k": p["k"], "files": p["file_count"], "est_tokens": p["tokens"], "diff": p["has_diff"],
                   "dirs": sorted(dir_counts(p["files"]))[:6], "zip_kb": os.path.getsize(p["zip"]) // 1024}
                  for p in rnd["parts"]],
        "skipped": {r: sum(1 for s in skipped if s["reason"] == r) for r in sorted({s["reason"] for s in skipped})},
        "possible_secrets_excluded": secrets,
        "oversized_files": oversized,
    }, ensure_ascii=False, indent=2))


def dir_counts(paths):
    out = {}
    for p in paths:
        d = p.rsplit("/", 1)[0] if "/" in p else "."
        out[d] = out.get(d, 0) + 1
    return out


# ---------------------------------------------------------------- prompts

def template(name):
    with open(os.path.join(HERE, "prompts", name), encoding="utf-8") as f:
        return string.Template(f.read())


def guidance(state):
    if state.get("brief"):
        with open(state["brief"], encoding="utf-8") as f:
            brief = f.read().strip()
        return ("## 审查说明（由项目方提供，审查范围和重点以此为准）\n\n" + brief +
                "\n\n> 注意：上面说明中如有自己的输出格式，请忽略，统一使用本消息最后规定的 JSON 输出格式。")
    return (f"## 项目背景\n{state['context'] or '（未提供，请从代码本身推断。）'}\n\n"
            f"## 审查重点\n{state['focus']}")


def status_table(state, k):
    rows = [f for f in state["findings"] if f.get("part") == k and f["status"] != "verified"]
    if not rows:
        return "（本部分上一轮没有待跟进的问题。）"
    out = ["| ID | 严重级别 | 问题 | 位置 | 处理 | 说明 |", "|---|---|---|---|---|---|"]
    for f in sorted(rows, key=sort_key):
        note = f.get("status_note") or (f.get("verdict_note") if f["status"] == "wontfix" else "") or ""
        out.append(f"| {f['id']} | {f['severity']} | {cell(f['title'])} | {cell(loc(f))} | "
                   f"{STATUS_ZH[f['status']]} | {cell(note)} |")
    verified = [f["id"] for f in state["findings"] if f.get("part") == k and f["status"] == "verified"]
    if verified:
        out += ["", f"此前已确认修复、无需再看：{', '.join(verified)}。"]
    return "\n".join(out)


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_prompt(state, rnd, part, handoff):
    k, total = part["k"], rnd["total_parts"]
    meta = state["parts"].get(str(k))
    part_note = ""
    if total > 1:
        part_note = (f"\n**这是第 {k}/{total} 部分。** 项目代码分成 {total} 部分，分别在不同对话中审查；"
                     "本对话只审本包内的文件。`REVIEW_MANIFEST.md` 末尾附有全项目文件清单供了解结构。"
                     "如果某个问题必须看其他部分的文件才能确认，照样报告，但在 problem 里写明需要哪个文件，"
                     "并把 confidence 设为 low。\n")
    diff_note = "；`CHANGES.diff` 是这些文件相对上次审查的改动，请重点看改动段落及其上下游" if part["has_diff"] else ""
    common = {"project": state["name"], "zip_name": os.path.basename(part["zip"]),
              "file_count": part["file_count"], "line_count": part["lines"], "round": rnd["n"],
              "part_note": part_note, "diff_note": diff_note, "guidance": guidance(state),
              "first_id": next_id(state)}
    if not meta:
        text = template("round1.md").safe_substitute(common)
    else:
        intro = ""
        if handoff:
            intro = ("这是一个新对话：这部分代码之前在另一个对话里审查过，下面是你需要的全部背景。\n\n"
                     + guidance(state) + "\n")
        text = template("followup.md").safe_substitute(common, handoff=intro, status_table=status_table(state, k))
    return re.sub(r"\n{3,}", "\n\n", text)


def write_prompt(state, rnd, part, handoff):
    path = os.path.join(part_dir(state, rnd, part["k"]), "prompt.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_prompt(state, rnd, part, handoff))
    part["prompt"] = path
    part["handoff"] = handoff
    return path


def cmd_prompt(args):
    state = load(args.root)
    rnd = current_round(state)
    if not rnd or not rnd["parts"]:
        sys.exit("error: run `review.py pack` first")
    parts = [get_part(rnd, args.part)] if args.part else [p for p in rnd["parts"] if not p.get("answer")]
    for p in parts:
        if args.handoff:
            state["parts"].setdefault(str(p["k"]), {"hashes": {}})["conversation"] = None
        print(write_prompt(state, rnd, p, args.handoff))
    save(state)


def cmd_next(args):
    state = load(args.root)
    rnd = current_round(state)
    if not rnd or not rnd["parts"]:
        sys.exit("error: run `review.py pack` first")
    todo = [p for p in rnd["parts"] if not p.get("answer")]
    if not todo:
        print(json.dumps({"done": True, "round": rnd["n"]}, ensure_ascii=False))
        return
    p = todo[0]
    meta = state["parts"].get(str(p["k"])) or {}
    conversation = meta.get("conversation")
    if not p.get("prompt") or (p.get("handoff") is False and meta and not conversation):
        write_prompt(state, rnd, p, handoff=bool(meta) and not conversation)
        save(state)
    out = os.path.join(part_dir(state, rnd, p["k"]), "answer.md")
    # A new chat gets a unique title of our own (ChatGPT's auto titles change and repeat).
    title = None if conversation else (
        f"review {state['name']}" + (f" p{p['k']}" if rnd["total_parts"] > 1 else "")
        + f" #{dt.datetime.now().strftime('%m%d-%H%M')}")
    print(json.dumps({"done": False, "round": rnd["n"], "part": p["k"], "of": rnd["total_parts"],
                      "remaining": len(todo), "zip": p["zip"], "prompt": p["prompt"], "out": out,
                      "conversation": conversation, "title": title, "est_tokens": p["tokens"]},
                     ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- ingest / update

def extract_json(text):
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    for candidate in reversed(blocks):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def cmd_ingest(args):
    state = load(args.root)
    rnd = current_round(state)
    if not rnd or not rnd["parts"]:
        sys.exit("error: no round in progress")
    part = get_part(rnd, args.part) if args.part else next((p for p in rnd["parts"] if not p.get("answer")), None)
    if part is None:
        sys.exit("error: every part of this round is already ingested (pass --part to redo one)")
    k = part["k"]
    with open(args.answer, encoding="utf-8") as f:
        text = f.read()
    data = extract_json(text)
    if data is None:
        sys.exit("error: no parsable JSON block in the answer. Ask ChatGPT (same conversation) to "
                 "re-output only the JSON block, then ingest that answer.")
    part["answer"] = os.path.abspath(args.answer)
    part["chatgpt_summary"] = data.get("summary") or ""
    part["chatgpt_overview"] = text.split("```", 1)[0].strip()
    part["unread_files"] = [str(x) for x in (data.get("unread_files") or [])]
    part["coverage"] = data.get("coverage") or {}
    meta = state["parts"].setdefault(str(k), {})
    meta_path = args.answer + ".meta.json"
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            m = json.load(f)
        part["conversation"] = m.get("title")
        part["usage"] = {key: m.get(key) for key in ("tokens_used", "budget", "turns")}
        meta["conversation"] = m.get("title")
    meta["hashes"] = part["hashes"]
    meta["last_round"] = rnd["n"]

    checks = []
    for c in data.get("fix_checks") or []:
        f = next((x for x in state["findings"] if x["id"] == c.get("id")), None)
        if not f:
            continue
        f["fix_check"] = {"round": rnd["n"], "status": c.get("status"), "note": c.get("note", "")}
        if f["status"] == "fixed":
            f["status"] = "verified" if c.get("status") == "fixed" else "open"
        checks.append(f"{f['id']}: {c.get('status')}")
    for d in data.get("disputes") or []:
        f = next((x for x in state["findings"] if x["id"] == d.get("id")), None)
        if f:
            f["chatgpt_reply"] = {"round": rnd["n"], "agree": d.get("agree"), "note": d.get("note", "")}

    added = []
    for raw in data.get("findings") or []:
        fid = str(raw.get("id") or "")
        if not re.fullmatch(r"F\d+", fid) or any(f["id"] == fid for f in state["findings"]):
            fid = next_id(state)
        sev = str(raw.get("severity", "medium")).lower()
        state["findings"].append({
            "id": fid, "round": rnd["n"], "part": k, "severity": sev if sev in SEVERITIES else "medium",
            "category": raw.get("category", ""), "file": raw.get("file", ""), "line": raw.get("line"),
            "title": str(raw.get("title", "")).strip() or "(untitled)", "evidence": raw.get("evidence", ""),
            "problem": raw.get("problem", ""), "suggestion": raw.get("suggestion", ""),
            "confidence": raw.get("confidence", ""), "verification": raw.get("verification", ""),
            "verdict": None, "verdict_note": "",
            "status": "open", "status_note": ""})
        added.append(fid)
    part["ingested"] = now()
    save(state)
    remaining = [p["k"] for p in rnd["parts"] if not p.get("answer")]
    print(json.dumps({"round": rnd["n"], "part": k, "conversation": meta.get("conversation"), "new_findings": added,
                      "by_severity": {s: sum(1 for f in state["findings"] if f["id"] in added and f["severity"] == s)
                                      for s in SEVERITIES},
                      "fix_checks": checks, "parts_remaining": remaining}, ensure_ascii=False, indent=2))


def cmd_set(args):
    state = load(args.root)
    for fid in args.ids:
        f = find(state, fid)
        if args.verdict:
            f["verdict"], f["verdict_note"] = args.verdict, args.note or f.get("verdict_note", "")
        if args.status:
            f["status"], f["status_note"] = args.status, args.note or f.get("status_note", "")
    save(state)
    print(f"updated {', '.join(args.ids)}")


def cmd_status(args):
    state = load(args.root)
    rnd = current_round(state)
    print(json.dumps({
        "project": state["name"], "root": state["root"], "source": state.get("source"),
        "state_dir": state_dir(state["root"]),
        "round": rnd and {"n": rnd["n"], "total_parts": rnd.get("total_parts"),
                          "parts": [{k: p.get(k) for k in ("k", "file_count", "tokens", "conversation", "answer")}
                                    for p in rnd["parts"]]},
        "unverified": [f["id"] for f in state["findings"] if f["status"] == "open" and not f.get("verdict")],
        "findings": [{k: f.get(k) for k in ("id", "part", "severity", "title", "verdict", "status")}
                     for f in state["findings"]],
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- report

def sort_key(f):
    return (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 9,
            int(f["id"][1:]) if f["id"][1:].isdigit() else 0)


def loc(f):
    return f"{f['file']}:{f['line']}" if f.get("line") else f.get("file", "")


def md(text):
    """Small Markdown subset: fenced code, inline code, bold, paragraphs."""
    out, pos = [], 0
    text = str(text or "")
    for m in re.finditer(r"```[^\n]*\n(.*?)(?:\n```|$)", text, re.S):
        out.append(_inline(text[pos:m.start()]))
        out.append(f"<pre><code>{html.escape(m.group(1))}</code></pre>")
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out)


def _inline(chunk):
    paras = [p.strip() for p in re.split(r"\n\s*\n", chunk) if p.strip()]
    rendered = []
    for p in paras:
        p = html.escape(p)
        p = re.sub(r"`([^`]+)`", r"<code>\1</code>", p)
        p = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", p)
        rendered.append("<p>" + p.replace("\n", "<br>") + "</p>")
    return "".join(rendered)


def card(f, number=None):
    e = html.escape
    extra = []
    if f.get("verdict") or f.get("verdict_note"):
        extra.append(f'<div class="note verdict-{e(f.get("verdict") or "none")}"><b>Claude 核实：'
                     f'{e(VERDICT_ZH.get(f.get("verdict"), "未核实"))}</b>{md(f.get("verdict_note"))}</div>')
    if f.get("status_note") and f["status"] != "open":
        extra.append(f'<div class="note"><b>处理：{e(STATUS_ZH[f["status"]])}</b>{md(f["status_note"])}</div>')
    if f.get("fix_check"):
        c = f["fix_check"]
        extra.append(f'<div class="note"><b>ChatGPT 第 {c["round"]} 轮核实修复：{e(str(c.get("status")))}</b>'
                     f'{md(c.get("note"))}</div>')
    if f.get("chatgpt_reply"):
        r = f["chatgpt_reply"]
        stance = "同意不修改" if r.get("agree") else "仍建议修改"
        extra.append(f'<div class="note warn"><b>ChatGPT 第 {r["round"]} 轮回复：{stance}</b>{md(r.get("note"))}</div>')
    head_num = f'<span class="num">{number}</span>' if number else ""
    evidence = f'<h4>证据</h4><pre><code>{e(str(f["evidence"]))}</code></pre>' if f.get("evidence") else ""
    part = f'<span>第 {f["part"]} 部分</span>' if f.get("part") else ""
    return f"""
<article class="card sev-{e(f['severity'])}" data-sev="{e(f['severity'])}">
  <header>
    {head_num}<span class="badge {e(f['severity'])}">{e(f['severity'])}</span>
    <span class="fid">{e(f['id'])}</span>
    <h3>{e(f['title'])}</h3>
  </header>
  <div class="meta"><code>{e(loc(f))}</code><span>{e(f.get('category') or '')}</span>
    <span>ChatGPT 置信度：{e(str(f.get('confidence') or '—'))}</span><span>第 {f['round']} 轮发现</span>{part}
    <span class="status">{e(STATUS_ZH[f['status']])}</span></div>
  <h4>问题</h4>{md(f.get('problem'))}
  {evidence}
  <h4>修改建议</h4>{md(f.get('suggestion'))}
  {('<h4>ChatGPT 的验证方式</h4>' + md(f.get('verification'))) if f.get('verification') else ''}
  {''.join(extra)}
</article>"""


def cmd_report(args):
    state = load(args.root)
    rnd = current_round(state)
    if not rnd:
        sys.exit("error: no rounds yet")
    if args.summary:
        rnd["claude_summary"] = args.summary
        save(state)
    e = html.escape
    fs = sorted(state["findings"], key=sort_key)
    todo = [f for f in fs if f["status"] == "open" and f.get("verdict") != "disputed"]
    done = [f for f in fs if f["status"] in ("fixed", "verified")]
    rest = [f for f in fs if f not in todo and f not in done]
    counts = {s: sum(1 for f in todo if f["severity"] == s) for s in SEVERITIES}
    answered = [p for p in rnd["parts"] if p.get("answer")]
    files = sum(p["file_count"] for p in rnd["parts"])
    lines = sum(p["lines"] for p in rnd["parts"])
    overview = "".join(
        f'<div class="part"><b>第 {p["k"]} 部分</b>' + (f'（{p["file_count"]} 个文件）' if rnd["total_parts"] > 1 else "")
        + md(p.get("chatgpt_overview") or p.get("chatgpt_summary") or "")
        + (f'<p class="sub">ChatGPT 说没读完：{e("、".join(p["unread_files"]))}</p>' if p.get("unread_files") else "")
        + (f'<p class="sub">覆盖情况：{e(json.dumps(p["coverage"], ensure_ascii=False))}</p>' if p.get("coverage") else "")
        + "</div>" for p in answered)
    usage_rows = "".join(
        f'<div class="usage"><span>{"第 %d 部分 · " % p["k"] if rnd["total_parts"] > 1 else ""}{e(p.get("conversation") or "—")}</span>'
        f'<span>本地估算 {(p.get("usage") or {}).get("tokens_used") or 0} / 预算 {(p.get("usage") or {}).get("budget") or 0} tokens（非实际上下文用量）</span>'
        f'<div class="bar"><i style="width:{min(100, round(100 * ((p.get("usage") or {}).get("tokens_used") or 0) / ((p.get("usage") or {}).get("budget") or 1)))}%"></i></div></div>'
        for p in answered)
    pending = [p["k"] for p in rnd["parts"] if not p.get("answer")]
    warning = (f'<div class="panel warnbox">本轮还有 {len(pending)} 个部分没有审完：'
               f'{", ".join(map(str, pending))}。下面的结论只覆盖已审完的部分。</div>') if pending else ""
    todo_html = "".join(card(f, i) for i, f in enumerate(todo, 1)) or '<p class="empty">没有待修改的问题。</p>'
    checklist = "\n".join(f"{i}. [{f['severity']}] {f['id']} {f['title']} — {loc(f)}" for i, f in enumerate(todo, 1))
    parts_note = (f" · 共 {rnd['total_parts']} 个部分，本轮送审 {len(rnd['parts'])} 个、已完成 {len(answered)} 个"
                  if rnd["total_parts"] > 1 else "")
    page = string.Template(REPORT).safe_substitute(
        project=e(state["name"]), round=rnd["n"], generated=e(now()), parts_note=e(parts_note),
        files=files, lines=lines, warning=warning,
        claude_summary=md(rnd.get("claude_summary") or "（尚未填写）"),
        overview=overview or '<p class="empty">暂无。</p>', usage=usage_rows,
        todo_count=len(todo), done_count=len(done), rest_count=len(rest),
        c_critical=counts["critical"], c_high=counts["high"], c_medium=counts["medium"], c_low=counts["low"],
        todo=todo_html, done="".join(card(f) for f in done) or '<p class="empty">暂无。</p>',
        rest="".join(card(f) for f in rest) or '<p class="empty">暂无。</p>',
        # Inside <script> entities are not decoded: JSON-encode only, and keep "</" from closing the tag.
        checklist=json.dumps(checklist, ensure_ascii=False).replace("</", "<\\/"))
    d = os.path.join(state_dir(state["root"]), f"round-{rnd['n']}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "report.html")
    for target in (path, os.path.join(state_dir(state["root"]), "report.html")):
        with open(target, "w", encoding="utf-8") as f:
            f.write(page)
    rnd["report"] = path
    save(state)
    print(path)


REPORT = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>代码审查报告</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;--code:#f2f4f7;
--critical:#b42318;--high:#d9480f;--medium:#b7791f;--low:#3b6fd8;--ok:#16794c;--accent:#4f46e5}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--panel:#1b1e23;--text:#e6e8eb;--muted:#9aa1ab;
--line:#2c3138;--code:#23272e;--critical:#f97066;--high:#fd853a;--medium:#fdb022;--low:#84adff;--ok:#47cd89;--accent:#a5b4fc}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 12px;display:flex;gap:8px;align-items:baseline}
h2 small{color:var(--muted);font-weight:400;font-size:14px}
.sub{color:var(--muted);font-size:13px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:16px}
.warnbox{border-color:var(--medium)}
.part+.part{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.usage{display:grid;grid-template-columns:1fr auto;gap:2px 12px;font-size:13px;color:var(--muted);margin-top:10px}.usage .bar{grid-column:1/-1}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:24px;line-height:1.2}.stat span{color:var(--muted);font-size:12px}
.bar{height:6px;background:var(--line);border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px}
.filters button,.copy{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:99px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.filters button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--muted);border-radius:12px;padding:16px 20px;margin:12px 0}
.card.sev-critical{border-left-color:var(--critical)}.card.sev-high{border-left-color:var(--high)}
.card.sev-medium{border-left-color:var(--medium)}.card.sev-low{border-left-color:var(--low)}
.card header{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.card h3{font-size:16px;margin:0;flex:1 1 300px}
.num{font-weight:700;color:var(--muted);min-width:1.6em}.fid{color:var(--muted);font-size:13px;font-family:ui-monospace,Menlo,monospace}
.badge{font-size:12px;font-weight:600;padding:1px 8px;border-radius:99px;color:#fff;text-transform:uppercase}
.badge.critical{background:var(--critical)}.badge.high{background:var(--high)}.badge.medium{background:var(--medium)}.badge.low{background:var(--low)}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:13px;margin:6px 0 4px}
.meta .status{color:var(--text)}h4{font-size:13px;color:var(--muted);margin:14px 0 4px;font-weight:600}
p{margin:4px 0}code{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:var(--code);padding:1px 5px;border-radius:5px;word-break:break-word}
pre{background:var(--code);border-radius:8px;padding:10px 12px;overflow:auto;margin:6px 0}pre code{padding:0;background:none;word-break:normal}
.note{margin-top:12px;padding:10px 12px;border-radius:8px;background:var(--code);font-size:14px}.note b{display:block;margin-bottom:2px}
.verdict-confirmed{box-shadow:inset 3px 0 var(--ok)}.verdict-disputed{box-shadow:inset 3px 0 var(--critical)}
.verdict-uncertain,.warn{box-shadow:inset 3px 0 var(--medium)}.empty{color:var(--muted)}
details summary{cursor:pointer;margin:36px 0 12px;font-size:19px;font-weight:700}
@media print{.filters,.copy{display:none}.card{break-inside:avoid}}
</style></head><body><main>
<h1>代码审查报告 · $project</h1>
<div class="sub">第 $round 轮 · 生成于 $generated · $files 个文件 / 约 $lines 行$parts_note</div>
$warning
<div class="stats">
  <div class="stat"><b>$todo_count</b><span>待修改</span></div>
  <div class="stat"><b style="color:var(--critical)">$c_critical</b><span>critical</span></div>
  <div class="stat"><b style="color:var(--high)">$c_high</b><span>high</span></div>
  <div class="stat"><b style="color:var(--medium)">$c_medium</b><span>medium</span></div>
  <div class="stat"><b style="color:var(--low)">$c_low</b><span>low</span></div>
  <div class="stat"><b style="color:var(--ok)">$done_count</b><span>已修复</span></div>
</div>

<div class="panel"><b>Claude 的总结</b>$claude_summary</div>
<div class="panel"><b>ChatGPT 的概述</b>$overview$usage</div>

<h2>要改的点 <small>$todo_count 项，按严重程度排序</small></h2>
<div class="filters"><button data-f="all" aria-pressed="true">全部</button><button data-f="critical">critical</button>
<button data-f="high">high</button><button data-f="medium">medium</button><button data-f="low">low</button>
<button class="copy" id="copy">复制修改清单</button></div>
<section id="todo">$todo</section>

<details><summary>已修复（$done_count）</summary>$done</details>
<details><summary>不修改 / 存疑（$rest_count）</summary>$rest</details>
</main>
<script>
const checklist = $checklist;
document.querySelectorAll('.filters button[data-f]').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.filters button[data-f]').forEach(x => x.setAttribute('aria-pressed', x === b));
  document.querySelectorAll('#todo .card').forEach(c => c.hidden = b.dataset.f !== 'all' && c.dataset.sev !== b.dataset.f);
}));
document.getElementById('copy').addEventListener('click', async e => {
  try { await navigator.clipboard.writeText(checklist); e.target.textContent = '已复制'; }
  catch { e.target.textContent = '复制失败'; }
});
</script></body></html>
"""


# ---------------------------------------------------------------- init

def cmd_init(args):
    root = os.path.abspath(args.root)
    sdir = state_dir(root)
    path = os.path.join(sdir, "state.json")
    if os.path.exists(path) and not args.force:
        sys.exit(f"error: {path} already exists (use --force to start over)")
    if args.force and os.path.isdir(sdir):
        shutil.rmtree(sdir)
    state = {"name": args.name or os.path.basename(root.rstrip("/")), "root": root,
             "focus": args.focus or DEFAULT_FOCUS, "context": args.context or "", "created": now(),
             "rounds": [], "findings": [], "parts": {}, "partition": {}, "include": args.include or []}
    if args.source_zip:
        # Review a prepared snapshot instead of the working tree; strip a shared top folder.
        src = os.path.join(sdir, "source")
        with zipfile.ZipFile(args.source_zip) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
            top = {n.split("/", 1)[0] for n in names}
            prefix = top.pop() + "/" if len(top) == 1 and all("/" in n for n in names) else ""
            for n in names:
                rel = n[len(prefix):]
                dest = os.path.join(src, rel)
                if not os.path.abspath(dest).startswith(os.path.abspath(src) + os.sep):
                    continue  # zip-slip guard
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as fin, open(dest, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
        state["source"] = src
        state["source_zip"] = os.path.abspath(args.source_zip)
        if not args.name:
            state["name"] = (prefix.rstrip("/") or os.path.splitext(os.path.basename(args.source_zip))[0])
    base = state.get("source") or root
    for key, given, patterns in (("brief", args.brief, ["00_审查说明.md", "REVIEW_BRIEF.md"]),
                                 ("diff", args.diff, ["改动.diff", "CHANGES.diff"])):
        candidate = given or next((os.path.join(base, p) for p in patterns if os.path.exists(os.path.join(base, p))), None)
        if candidate:
            state[key] = os.path.abspath(candidate)
            if os.path.abspath(candidate).startswith(os.path.abspath(base) + os.sep):
                state[key + "_rel"] = os.path.relpath(candidate, base)
    os.makedirs(sdir, exist_ok=True)
    save(state)
    print(json.dumps({"state_dir": sdir, "name": state["name"], "source": state.get("source"),
                      "include": state["include"],
                      "brief": state.get("brief"), "diff": state.get("diff")}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        p = sub.add_parser(name)
        p.add_argument("--root", default=os.getcwd(), type=os.path.abspath)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init)
    p.add_argument("--name")
    p.add_argument("--focus")
    p.add_argument("--context")
    p.add_argument("--brief", help="review instructions to use instead of context/focus")
    p.add_argument("--source-zip", help="review this snapshot zip instead of the working tree")
    p.add_argument("--diff", help="diff of the changes since the last review (split per part)")
    p.add_argument("--include", nargs="+", metavar="PATH",
                   help="only review these directories / files / globs (relative to the root), every round")
    p.add_argument("--force", action="store_true")
    p = add("pack", cmd_pack)
    p.add_argument("--chunk-tokens", type=int, default=0,
                   help="split into parts of at most this many estimated tokens (0 = one package)")
    p.add_argument("--include-locks", action="store_true")
    p.add_argument("--max-file-kb", type=int, default=512)
    add("next", cmd_next)
    p = add("prompt", cmd_prompt)
    p.add_argument("--part", type=int)
    p.add_argument("--handoff", action="store_true", help="the prompt starts a new chat for a reviewed part")
    p = add("ingest", cmd_ingest)
    p.add_argument("answer")
    p.add_argument("--part", type=int)
    p = add("set", cmd_set)
    p.add_argument("ids", nargs="+")
    p.add_argument("--verdict", choices=VERDICTS)
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--note")
    p = add("report", cmd_report)
    p.add_argument("--summary")
    add("status", cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
