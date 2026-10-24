#!/usr/bin/env python3
"""Minimal harness for testing changes to SANA OS text on the reference composition.

    python sana_eval.py compose --sana-os ../sana-os --ref main --persona coaching
    python sana_eval.py run --sana-os ../sana-os --variant before=main --variant after=t7/scope \\
        --cases cases/pr4-t7.json --config config.json --label pr4
    python sana_eval.py run ... --dry-run          # compose and list the calls, send nothing
    python sana_eval.py report runs/<run>          # rebuild compare.md and review.md

What it does, and why each part is there:
  * Composes each persona exactly as personas/README.md "Reference composition" says
    (sana-os decision 0003), reading the files of a given commit with `git show`, so
    two commits can be compared without checking either out.
  * Sends the same inputs to one OpenAI-compatible endpoint (llama-server, or any other)
    with the same settings, a fixed number of repetitions per variant. Repetition i uses
    seed + i for every variant, so before and after are paired.
  * Records everything needed to repeat or question a result: the commits, the composed
    prompt and its hash, the endpoint settings (without the key), every request and reply.
  * Writes compare.md (variants side by side) and review.md (what to look at, per case).
    It does not judge. Judging is a separate role: the author, or a reviewer.
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
        "endpoint": public_config(cfg), "repeat": repeat,
        "cases_file": {"path": os.path.relpath(cases_path, HERE), "sha256": sha256(cases_path.read_text(encoding="utf-8"))},
        "skipped": skipped,
    }
    write_json(out / "run.json", run)

    seed0 = cfg.get("seed")
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
                status = "error: " + record["turns"][-1]["error"] if "error" in record["turns"][-1] else "ok"
                print(f"  {c['id']:<28} {vname:<8} rep {rep}  {status}")
    run["ended"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(out / "run.json", run)
    report(out, cases)
    print(f"\nWritten: {out}\n  compare.md — variants side by side\n  review.md  — what to look at, to fill in")
    return 0


# ---------------------------------------------------------------- report

def report(out: Path, cases: dict | None = None) -> None:
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    if cases is None:
        cases = json.loads((HERE / run["cases_file"]["path"]).read_text(encoding="utf-8"))
    variants = list(run["variants"])
    head = [f"# {run['label']}", "",
            f"- 開始 {run['started']}　終了 {run.get('ended', '（未完了）')}　ハーネス `{run['harness']}`",
            f"- エンドポイント `{run['endpoint'].get('base_url')}`　モデル `{run['endpoint'].get('model')}`　"
            f"設定 `{json.dumps(run['endpoint'].get('params', {}), ensure_ascii=False)}`　"
            f"seed `{run['endpoint'].get('seed')}`+回数　"
            f"extra_body `{json.dumps(run['endpoint'].get('extra_body', {}), ensure_ascii=False)}`",
            f"- 繰り返し {run['repeat']} 回（同じ回数目は同じ seed）",
            "- 比較するもの："]
    for v, info in run["variants"].items():
        head.append(f"  - **{v}** = `{info['ref']}` `{info['commit'][:12]}` {info['subject']}")
    for v, ps in run["prompts"].items():
        for p, x in ps.items():
            head.append(f"  - {v} / {p}: {x['chars']:,} 文字, sha256 `{x['sha256'][:16]}`")
    if run.get("skipped"):
        head.append("- 実行しなかったケース（入力が未記入）：" + ", ".join(run["skipped"]))
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
            review.append(f"- {rep} 回目：" + "　".join(f"{v} [ ]" for v in variants) + "　気づいた差：")
        review += ["", "**まとめ**（変更前後で何が変わったか／変わらなかったか）：", "", "---", ""]
        compare += ["---", ""]
    for name, lines in (("compare.md", compare), ("review.md", review)):
        with open(out / name, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines).rstrip("\n") + "\n")


def cmd_report(args) -> int:
    report(Path(args.run))
    print(f"Rewritten: {args.run}/compare.md, review.md")
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
    p = sub.add_parser("report", help="rewrite compare.md and review.md of a run")
    p.add_argument("run")
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
