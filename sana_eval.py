#!/usr/bin/env python3
"""Minimal harness for testing changes to SANA OS text on the reference composition.

    python sana_eval.py compose --sana-os ../sana-os --ref main --persona coaching
    python sana_eval.py run --sana-os ../sana-os --variant before=main --variant after=t7/scope \\
        --cases cases/pr4-t7.json --config config.json --label pr4
    python sana_eval.py run ... --dry-run          # compose and list the calls, send nothing
    python sana_eval.py report runs/<run>          # rebuild compare.md and review.template.md
    python sana_eval.py report runs/<run> --cases cases/x.json --reason "..."   # re-review, new look-for

What it does, and why each part is there:
  * Composes each persona exactly as personas/README.md "Reference composition" says
    (sana-os decision 0003), reading the files of a given commit with `git show`, so
    two commits can be compared without checking either out.
  * Sends the same inputs to one OpenAI-compatible endpoint (llama-server, or any other)
    with the same settings, a fixed number of repetitions per variant. Repetition i uses
    seed + i for every variant, so before and after are paired.
  * Records everything needed to repeat or question a result: the commits, the composed
    prompt and its hash, the endpoint settings (without the key), every request and reply.
  * Writes compare.md (variants side by side, with finish reason and token counts) and
    review.template.md (what to look at, per case). review.md is created once, as a copy of
    the template, for the reader to fill in; no later command overwrites it.
    It does not judge. Judging is a separate role: the author, or a reviewer.
  * Keeps the cases file as it was at run time (cases.snapshot.json), so a report always
    shows the look-for points the run was made with. Reading a run against changed
    look-for points is a re-review: it needs a reason, is recorded in run.json, and goes
    to separate files.
  * Stops when the endpoint cannot be reached: at the first request, or after three
    failures in a row, instead of trying every remaining request.
Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILTIN_PERSONAS = {"coaching": "Persona_Coaching", "history": "Persona_History",
                    "reasoning": "Persona_Reasoning", "template": "Persona_Template"}
PLACEHOLDER = "TODO:"


# ---------------------------------------------------------------- git

def git(repo: Path, *args: str) -> str:
    # UTF-8 explicitly: the files are UTF-8 whatever the locale (cp932 on Japanese Windows).
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          encoding="utf-8", errors="strict").stdout


def resolve(repo: Path, ref: str) -> dict:
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    date = git(repo, "log", "-1", "--format=%cI", commit).strip()
    subject = git(repo, "log", "-1", "--format=%s", commit).strip()
    return {"ref": ref, "commit": commit, "date": date, "subject": subject}


def show(repo: Path, commit: str, path: str) -> str:
    return git(repo, "show", f"{commit}:{path}")


def harness_version() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE), "describe", "--always", "--dirty", "--tags"],
                              capture_output=True, check=True, encoding="utf-8").stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- composition (decision 0003)

def loading_order(readme: str, persona_file: str) -> list[str]:
    """The numbered list in the first code block after '### <persona_file>' in personas/README.md."""
    m = re.search(rf"^### {re.escape(persona_file)}\s*$(.*?)^```[^\n]*\n(.*?)^```", readme, flags=re.M | re.S)
    if not m:
        raise SystemExit(f"personas/README.md has no loading order for {persona_file}")
    order = []
    for line in m.group(2).splitlines():
        item = re.match(r"\s*\d+\.\s*(\S+)", line)
        if item:
            order.append(item.group(1))
    return order


def template_persona(template: str, sections: str) -> str:
    """Persona_Template.md with the developer note removed and sections 2-3 replaced."""
    cut = re.search(r"^## 2\.", template, flags=re.M)
    if not cut:
        raise SystemExit("Persona_Template.md has no '## 2.' heading")
    head = "\n".join(l for l in template[:cut.start()].splitlines() if not re.match(r"^\[Developer Note.*\]\s*$", l))
    head = re.sub(r"\n{3,}", "\n\n", head)  # the removed line leaves no extra blank line
    return head.rstrip("\n") + "\n\n" + sections.strip("\n")


def compose(repo: Path, commit: str, spec: dict, base_dir: Path) -> tuple[str, list[str]]:
    """Reference composition: files in order, trailing newlines removed, joined by one blank line."""
    readme = show(repo, commit, "personas/README.md")
    persona_file = BUILTIN_PERSONAS[spec["base"]]
    order = loading_order(readme, persona_file)
    files: list[str] = []
    parts: list[str] = []
    for item in order:
        if item.startswith("["):  # "[frameworks as needed]" in the Template's order
            for f in spec.get("frameworks", []):
                files.append(f)
                parts.append(show(repo, commit, f).rstrip("\n"))
            continue
        files.append(item)
        text = show(repo, commit, item)
        if spec["base"] == "template" and item == "personas/Persona_Template.md":
            sections = (base_dir / spec["sections"]).read_text(encoding="utf-8")
            text = template_persona(text, sections)
            files[-1] = f"{item} (sections 2-3: {spec['sections']})"
        parts.append(text.rstrip("\n"))
    return "\n\n".join(parts), files


def persona_specs(cases: dict) -> dict:
    specs = {name: {"base": name} for name in BUILTIN_PERSONAS if name != "template"}
    specs.update(cases.get("personas", {}))
    return specs


# ---------------------------------------------------------------- endpoint

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in ("base_url", "model"):
        if key not in cfg:
            raise SystemExit(f"{path}: '{key}' is required")
    return cfg


def api_key(cfg: dict) -> str:
    if cfg.get("api_key_env"):
        return os.environ.get(cfg["api_key_env"], "")
    return cfg.get("api_key", "")


def public_config(cfg: dict) -> dict:
    """The settings as recorded: the key itself is never written."""
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    if "api_key" in cfg:
        out["api_key"] = "(set)"
    return out


def server_info(cfg: dict) -> dict:
    """What the server says about itself, best effort: /v1/models (any OpenAI-compatible
    server) and, for llama-server, /props (model file, context size, build, chat template).
    Why: the model name in the request does not identify the weights, quantisation or
    server settings that answered, and replies to the same prompt and seed can differ
    between runs; this is what can be recorded to look into such differences."""
    headers = {"Authorization": f"Bearer {api_key(cfg)}"} if api_key(cfg) else {}
    base = cfg["base_url"].rstrip("/")
    info: dict = {}
    for key, url in (("models", base + "/models"), ("props", re.sub(r"/v1$", "", base) + "/props")):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                info[key] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - recorded, not fatal
            info[key] = {"unavailable": f"{type(e).__name__}: {e}"}
    props = info.get("props", {})
    if isinstance(props.get("chat_template"), str):  # long; the hash identifies it
        props["chat_template_sha256"] = sha256(props.pop("chat_template"))
    return info


def chat(cfg: dict, messages: list[dict], seed: int | None) -> dict:
    body = {"model": cfg["model"], "messages": messages, **cfg.get("params", {})}
    if seed is not None:
        body["seed"] = seed
    body.update(cfg.get("extra_body", {}))
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key(cfg):
        headers["Authorization"] = f"Bearer {api_key(cfg)}"
    last_error = None
    for attempt in range(cfg.get("retries", 3)):
        start = time.monotonic()
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=cfg.get("timeout", 600)) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choice = payload["choices"][0]
            msg = choice.get("message", {})
            return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content"),
                    "finish_reason": choice.get("finish_reason"), "usage": payload.get("usage"),
                    "model_reported": payload.get("model"), "seconds": round(time.monotonic() - start, 2),
                    "attempts": attempt + 1}
        except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, json.JSONDecodeError) as e:
            last_error = f"{type(e).__name__}: {e}"
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                break
            time.sleep(min(30, 2 ** attempt * 3))
    return {"error": last_error}


# ---------------------------------------------------------------- inputs

def user_message(turn, base_dir: Path) -> dict:
    """A turn is a string, or {"text": ..., "images": [paths relative to the cases file]}."""
    if isinstance(turn, str):
        return {"role": "user", "content": turn}
    parts = [{"type": "text", "text": turn["text"]}]
    for img in turn.get("images", []):
        path = base_dir / img
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return {"role": "user", "content": parts}


def missing_inputs(case: dict, base_dir: Path) -> list[str]:
    """Placeholders the author has to fill (texts not in this repository, images)."""
    missing = []
    for turn in case["turns"]:
        text = turn if isinstance(turn, str) else turn["text"]
        if text.startswith(PLACEHOLDER):
            missing.append(text)
        if not isinstance(turn, str):
            missing += [f"image not found: {i}" for i in turn.get("images", []) if not (base_dir / i).is_file()]
    return missing


def turn_text(turn) -> str:
    return turn if isinstance(turn, str) else turn["text"] + "".join(f"\n[image: {i}]" for i in turn.get("images", []))


# ---------------------------------------------------------------- run

def cmd_compose(args) -> int:
    repo = Path(args.sana_os).resolve()
    commit = resolve(repo, args.ref)["commit"]
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8")) if args.cases else {}
    spec = persona_specs(cases)[args.persona]
    text, files = compose(repo, commit, spec, Path(args.cases).resolve().parent if args.cases else HERE)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text + "\n")
    print(f"\n# {args.persona} at {commit[:12]}: {len(text):,} chars, sha256 {sha256(text)[:16]}", file=sys.stderr)
    print("# files: " + ", ".join(files), file=sys.stderr)
    return 0


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def cmd_run(args) -> int:
    repo = Path(args.sana_os).resolve()
    cases_path = Path(args.cases).resolve()
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    base_dir = cases_path.parent
    specs = persona_specs(cases)
    repeat = args.repeat or cases.get("repeat", 3)
    variants = {}
    for v in args.variant:
        name, _, ref = v.partition("=")
        if not ref:
            raise SystemExit(f"--variant {v}: use name=ref, e.g. before=main")
        variants[name] = resolve(repo, ref)

    selected = [c for c in cases["cases"] if not args.only or c["id"] in args.only]
    runnable, skipped = [], {}
    for c in selected:
        if c["persona"] not in specs:
            raise SystemExit(f"case {c['id']}: unknown persona {c['persona']}")
        miss = missing_inputs(c, base_dir)
        if miss:
            skipped[c["id"]] = miss
        else:
            runnable.append(c)

    # Compose every persona used, for every variant, before anything is sent.
    prompts: dict = {}
    for vname, info in variants.items():
        for pname in sorted({c["persona"] for c in runnable}):
            text, files = compose(repo, info["commit"], specs[pname], base_dir)
            prompts.setdefault(vname, {})[pname] = {"text": text, "files": files,
                                                    "sha256": sha256(text), "chars": len(text)}

    print(f"cases: {len(runnable)} to run, {len(skipped)} skipped; repeat {repeat}; variants: "
          + ", ".join(f"{k}={v['ref']} ({v['commit'][:12]})" for k, v in variants.items()))
    for cid, miss in skipped.items():
        print(f"  skipped {cid}: " + "; ".join(m[:80] for m in miss))
    for vname, ps in prompts.items():
        for pname, p in ps.items():
            print(f"  {vname:<8} {pname:<12} {p['chars']:>7,} chars  sha256 {p['sha256'][:16]}")
    same = [p for p in next(iter(prompts.values()), {}) if len({prompts[v][p]["sha256"] for v in prompts}) == 1]
    if len(prompts) > 1 and same:
        print("  note: identical prompts in every variant for: " + ", ".join(same))
    calls = sum(len(c["turns"]) for c in runnable) * repeat * len(variants)
    print(f"  requests: {calls}")
    if args.dry_run:
        return 0

    cfg = load_config(Path(args.config))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out = Path(args.out or HERE / "runs") / f"{stamp}_{args.label}"
    out.mkdir(parents=True)
    for vname, ps in prompts.items():
        for pname, p in ps.items():
            (out / "prompts" / vname).mkdir(parents=True, exist_ok=True)
            with open(out / "prompts" / vname / f"{pname}.md", "w", encoding="utf-8", newline="\n") as fh:
                fh.write(p["text"])
    run = {
        "label": args.label, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "harness": harness_version(), "sana_os": str(repo), "variants": variants,
        "prompts": {v: {p: {k: x[k] for k in ("files", "sha256", "chars")} for p, x in ps.items()}
                    for v, ps in prompts.items()},
        "endpoint": public_config(cfg), "server": server_info(cfg), "repeat": repeat,
        "cases_file": {"path": os.path.relpath(cases_path, HERE), "sha256": sha256(cases_path.read_text(encoding="utf-8"))},
        "skipped": skipped,
    }
    write_json(out / "run.json", run)
    # The cases exactly as run: reports are built from this copy, never from a later edit.
    (out / "cases.snapshot.json").write_bytes(cases_path.read_bytes())

    seed0 = cfg.get("seed")
    succeeded, failed_in_row = 0, 0
    try:
        for c in runnable:
            for rep in range(1, repeat + 1):
                for vname in variants:  # interleaved, so drift on the server affects every variant alike
                    messages = [{"role": "system", "content": prompts[vname][c["persona"]]["text"]}]
                    record = {"case": c["id"], "variant": vname, "rep": rep,
                              "seed": None if seed0 is None else seed0 + rep, "turns": []}
                    for turn in c["turns"]:
                        messages.append(user_message(turn, base_dir))
                        reply = chat(cfg, messages, record["seed"])
                        record["turns"].append({"user": turn_text(turn), **reply})
                        if "error" in reply:
                            break
                        messages.append({"role": "assistant", "content": reply["content"]})
                    write_json(out / "transcripts" / c["id"] / f"{vname}-{rep}.json", record)
                    last = record["turns"][-1]
                    if "error" in last:
                        failed_in_row += 1
                        status = "error: " + last["error"]
                    else:
                        succeeded, failed_in_row = succeeded + 1, 0
                        status = "ok" if last.get("finish_reason") != "length" else "ok (cut at max_tokens)"
                    print(f"  {c['id']:<28} {vname:<8} rep {rep}  {status}")
                    if failed_in_row and (succeeded == 0 or failed_in_row >= 3):
                        raise Abort(last["error"])
    except Abort as e:
        run["aborted"] = f"stopped: {'the first request failed' if succeeded == 0 else 'three failures in a row'} ({e})"
        print(f"\nStopped. {run['aborted']}\nCheck base_url and the server in {args.config}, then run again.")
    run["ended"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(out / "run.json", run)
    report(out)
    print(f"\nWritten: {out}\n  compare.md          — variants side by side\n"
          f"  review.md           — what to look at, to fill in (never overwritten)")
    return 1 if "aborted" in run else 0


class Abort(Exception):
    pass


# ---------------------------------------------------------------- report

def run_cases(out: Path, run: dict) -> dict:
    """The cases the run was made with: the snapshot, or, for runs made before snapshots
    existed, the cases file only if it still has the recorded hash."""
    snap = out / "cases.snapshot.json"
    if snap.is_file():
        text = snap.read_text(encoding="utf-8")
        if sha256(text) != run["cases_file"]["sha256"]:
            raise SystemExit(f"{snap} does not have the hash recorded in run.json; it was changed after the run. "
                             "Restore it, or read the run with --cases and --reason (a re-review).")
        return json.loads(text)
    path = HERE / run["cases_file"]["path"]
    if path.is_file() and sha256(path.read_text(encoding="utf-8")) == run["cases_file"]["sha256"]:
        snap.write_bytes(path.read_bytes())  # unchanged since the run: keep that state from now on
        return json.loads(path.read_text(encoding="utf-8"))
    # Changed since: look for the version with the recorded hash in this repository's history.
    rel = run["cases_file"]["path"].replace("\\", "/")
    try:
        commits = git(HERE, "log", "--format=%H", "--", rel).split()
    except Exception:
        commits = []
    for commit in commits:
        try:
            text = git(HERE, "show", f"{commit}:{rel}")
        except Exception:
            continue
        if sha256(text) == run["cases_file"]["sha256"]:
            with open(snap, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print(f"cases.snapshot.json recovered from commit {commit[:12]} (hash matches the run)")
            return json.loads(text)
    raise SystemExit(f"{out} has no cases.snapshot.json, and {path} has changed since the run. "
                     "To read the run with the current cases, use --cases and --reason (a re-review).")


def report(out: Path, cases: dict | None = None, rereview: dict | None = None) -> None:
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    if cases is None:
        cases = run_cases(out, run)
    variants = list(run["variants"])
    head = [f"# {run['label']}", "",
            f"- 開始 {run['started']}　終了 {run.get('ended', '（未完了）')}　ハーネス `{run['harness']}`",
            f"- エンドポイント `{run['endpoint'].get('base_url')}`　モデル `{run['endpoint'].get('model')}`　"
            f"設定 `{json.dumps(run['endpoint'].get('params', {}), ensure_ascii=False)}`　"
            f"seed `{run['endpoint'].get('seed')}`+回数　"
            f"extra_body `{json.dumps(run['endpoint'].get('extra_body', {}), ensure_ascii=False)}`",
            f"- 繰り返し {run['repeat']} 回（同じ回数目は同じ seed）",
            "- 比較するもの："]
    server = run.get("server", {})
    props = server.get("props", {})
    if props and "unavailable" not in props:
        dgs = props.get("default_generation_settings", {})
        head.append(f"- サーバー：model_path `{props.get('model_path')}`　n_ctx `{dgs.get('n_ctx', props.get('n_ctx'))}`　"
                    f"slots `{props.get('total_slots')}`　build `{props.get('build_info')}`　"
                    f"chat_template sha256 `{str(props.get('chat_template_sha256', ''))[:16]}`")
    elif server:
        ids = [m.get("id") for m in server.get("models", {}).get("data", []) if isinstance(m, dict)]
        head.append(f"- サーバー：/props は取得できず。/models の id：{', '.join(map(str, ids)) or '取得できず'}")
    for v, info in run["variants"].items():
        head.append(f"  - **{v}** = `{info['ref']}` `{info['commit'][:12]}` {info['subject']}")
    for v, ps in run["prompts"].items():
        for p, x in ps.items():
            head.append(f"  - {v} / {p}: {x['chars']:,} 文字, sha256 `{x['sha256'][:16]}`")
    if run.get("skipped"):
        head.append("- 実行しなかったケース（入力が未記入）：" + ", ".join(run["skipped"]))
    if run.get("aborted"):
        head.append(f"- **途中で止まった実行**：{run['aborted']}")
    cut = [f"{p.parent.name}/{p.stem}" for p in sorted((out / "transcripts").rglob("*.json"))
           if any(t.get("finish_reason") == "length" for t in json.loads(p.read_text(encoding="utf-8"))["turns"])]
    head.append("- max_tokens で途中で切れた応答：" + (", ".join(cut) if cut else "なし"))
    if rereview:
        head.append(f"- **再レビュー**（実行時とは別の「見る点」で読む）：{rereview['at']}　ケース `{rereview['cases_path']}` "
                    f"sha256 `{rereview['sha256'][:16]}`　理由：{rereview['reason']}")
    head.append("")

    compare, review = list(head), list(head)
    review += ["判定はこのハーネスの役割ではありません。読む人（作者・レビュアー）が、ケースごとに記入します。", ""]
    for c in cases["cases"]:
        tdir = out / "transcripts" / c["id"]
        if not tdir.is_dir():
            continue
        block = [f"## {c['id']}（{c['persona']}）", ""]
        if c.get("purpose"):
            block += [c["purpose"], ""]
        if c.get("source"):
            block += [f"入力の出典：{c['source']}", ""]
        if c.get("reference"):
            block += [f"**参考**（判定の基準ではなく、以前の記録）：{c['reference']}", ""]
        block += ["**見る点**", ""] + [f"- {x}" for x in c.get("look_for", [])] + [""]
        compare += block
        review += block
        for rep in range(1, run["repeat"] + 1):
            for v in variants:
                path = tdir / f"{v}-{rep}.json"
                if not path.is_file():
                    continue
                rec = json.loads(path.read_text(encoding="utf-8"))
                compare.append(f"### {rep} 回目 — {v}")
                compare.append("")
                for t in rec["turns"]:
                    compare += ["> " + line for line in t["user"].splitlines()] + [""]
                    compare.append(t.get("content") or f"**エラー** {t.get('error')}")
                    compare.append("")
                    if "error" not in t:
                        u = t.get("usage") or {}
                        flag = "　**max_tokens で途中で切れています**" if t.get("finish_reason") == "length" else ""
                        compare += [f"<sub>finish_reason {t.get('finish_reason')}　prompt {u.get('prompt_tokens')}・"
                                    f"completion {u.get('completion_tokens')} tokens　model {t.get('model_reported')}　"
                                    f"{t.get('seconds')} s</sub>{flag}", ""]
            review.append(f"- {rep} 回目：" + "　".join(f"{v} [ ]" for v in variants) + "　気づいた差：")
        review += ["", "**まとめ**（変更前後で何が変わったか／変わらなかったか）：", "", "---", ""]
        compare += ["---", ""]
    suffix = "" if rereview is None else f".rereview-{rereview['stamp']}"
    for name, lines in ((f"compare{suffix}.md", compare), (f"review{suffix}.template.md", review)):
        with open(out / name, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines).rstrip("\n") + "\n")
    # The sheet the reader fills in is created once and never overwritten.
    filled = out / f"review{suffix}.md"
    if not filled.exists():
        filled.write_bytes((out / f"review{suffix}.template.md").read_bytes())


def cmd_report(args) -> int:
    out = Path(args.run)
    if args.cases:
        if not args.reason:
            raise SystemExit("--cases makes this a re-review with other look-for points; give --reason")
        path = Path(args.cases)
        now = datetime.now(timezone.utc)
        rr = {"at": now.isoformat(timespec="seconds"), "stamp": now.strftime("%Y%m%d-%H%M%SZ"),
              "cases_path": str(path), "sha256": sha256(path.read_text(encoding="utf-8")), "reason": args.reason}
        # Keep the look-for points of this re-review with the run, as they were read.
        rr["cases_copy"] = f"cases.rereview-{rr['stamp']}.json"
        (out / rr["cases_copy"]).write_bytes(path.read_bytes())
        run = json.loads((out / "run.json").read_text(encoding="utf-8"))
        run.setdefault("rereviews", []).append(rr)
        write_json(out / "run.json", run)
        report(out, json.loads(path.read_text(encoding="utf-8")), rereview=rr)
        print(f"Re-review written: compare.rereview-{rr['stamp']}.md, review.rereview-{rr['stamp']}.md, "
              f"{rr['cases_copy']} (recorded in run.json)")
        return 0
    report(out)
    print(f"Rewritten: {out}/compare.md, review.template.md (review.md is kept as it is)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose", help="print the reference composition of a persona at a commit")
    c.add_argument("--sana-os", required=True)
    c.add_argument("--ref", required=True)
    c.add_argument("--persona", required=True, help="coaching, history, reasoning, or a persona defined in --cases")
    c.add_argument("--cases", help="cases file that defines Template-based personas")
    c.add_argument("--out")
    c.set_defaults(func=cmd_compose)
    r = sub.add_parser("run", help="run cases against variants and record everything")
    r.add_argument("--sana-os", required=True, help="a clone of sana-os/sana-os that has the refs to compare")
    r.add_argument("--variant", action="append", required=True, help="name=ref, e.g. before=main (repeatable)")
    r.add_argument("--cases", required=True)
    r.add_argument("--config", help="endpoint settings (see config.example.json); not needed with --dry-run")
    r.add_argument("--label", default="run")
    r.add_argument("--repeat", type=int)
    r.add_argument("--only", nargs="*", help="run only these case ids")
    r.add_argument("--out", help="parent folder for the run (default: runs/)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report", help="rewrite compare.md and review.template.md of a run")
    p.add_argument("run")
    p.add_argument("--cases", help="read the run against another cases file (a re-review)")
    p.add_argument("--reason", help="why the run is read with other look-for points (required with --cases)")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    if args.cmd == "run" and not args.dry_run and not args.config:
        ap.error("--config is required unless --dry-run")
    return args.func(args)


if __name__ == "__main__":
    # Console or pipe in a legacy code page (cp932 on Japanese Windows): replace what it
    # cannot show (such as an em dash) instead of stopping with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    sys.exit(main())
