#!/usr/bin/env python3
"""Self-test: a small fake sana-os repository, a mock OpenAI-compatible server, one full run.

    python tests/selftest.py

Checks the parts a wrong result would be hardest to notice in:
  * the reference composition (order, trailing newlines removed, one blank line, nothing added)
  * the Template persona (developer note removed, sections 2-3 replaced, title and 0-1 kept)
  * the two variants read different commits; repetition i has the same seed in every variant
  * multi-turn: the assistant's reply is sent back in the next request
  * a case with a TODO placeholder is skipped and listed
  * the API key is never written to the run folder
  * a filled-in review.md survives `report`; reports use the cases snapshot, not a later edit;
    a re-review needs a reason and goes to separate files
  * a reply cut at max_tokens is flagged
  * an unreachable endpoint stops the run at the first request
Standard library only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SECRET = "sk-selftest-do-not-write"
REQUESTS: list[dict] = []


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        REQUESTS.append({"auth": self.headers.get("Authorization"), "body": body})
        system = body["messages"][0]["content"]
        last = body["messages"][-1]["content"]
        reply = f"echo seed={body.get('seed')} sys={system[:12]!r} turn={len(body['messages']) // 2} last={last}"
        finish = "length" if "LONG" in str(last) else "stop"
        out = json.dumps({"model": body["model"], "choices": [{"message": {"content": reply}, "finish_reason": finish}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802 - /v1/models answers, /props does not (like a non-llama server)
        if self.path.endswith("/models"):
            out = json.dumps({"data": [{"id": "mock-model"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def fake_sana_os(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@t")
    write(root / "personas/README.md", "# Personas\n\n### Persona_Coaching\n\n```\n1. core/A.md\n2. core/B.md\n"
          "3. personas/Persona_Coaching.md\n```\n\n### Persona_Template\n\n```\n1. core/A.md\n2. [frameworks as needed]\n"
          "3. personas/Persona_Template.md  ← customise Section 2\n```\n")
    write(root / "core/A.md", "Core A — 原則\n\n\n")
    write(root / "core/B.md", "Core B\n")
    write(root / "frameworks/F.md", "Framework F\n")
    write(root / "personas/Persona_Coaching.md", "Coaching v1\n")
    write(root / "personas/Persona_Template.md", "# Template (v1.0)\n[Developer Note: replace brackets.]\n\n"
          "## 0. HEAD v1\nrule\n\n## 1. ROOT\nseq\n\n## 2. CUSTOM\n[placeholder]\n\n## 3. UI\n[placeholder]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v1")
    git(root, "branch", "v1")
    write(root / "personas/Persona_Coaching.md", "Coaching v2\n")
    write(root / "personas/Persona_Template.md", "# Template (v1.0)\n[Developer Note: replace brackets.]\n\n"
          "## 0. HEAD v2\nrule\n\n## 1. ROOT\nseq\n\n## 2. CUSTOM\n[placeholder]\n\n## 3. UI\n[placeholder]\n")
    git(root, "commit", "-q", "-am", "v2")
    git(root, "branch", "v2")


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        print(("ok    " if cond else "FAIL  ") + what)
        if not cond:
            failures.append(what)

    server = HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        so = tmp / "sana-os"
        so.mkdir()
        fake_sana_os(so)
        write(tmp / "fx.md", "## 2. CUSTOM\nmine\n\n## 3. UI\nmine too\n")
        cases = {"repeat": 2, "personas": {"t": {"base": "template", "sections": "fx.md", "frameworks": ["frameworks/F.md"]}},
                 "cases": [{"id": "c1", "persona": "coaching", "turns": ["一つ目", "二つ目"], "look_for": ["x"]},
                           {"id": "t1", "persona": "t", "turns": ["hello LONG"], "look_for": ["y"]},
                           {"id": "todo", "persona": "coaching", "turns": ["TODO: paste"], "look_for": []}]}
        write(tmp / "cases.json", json.dumps(cases, ensure_ascii=False))
        write(tmp / "config.json", json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                               "model": "mock", "api_key": SECRET, "seed": 100,
                                               "params": {"temperature": 0.7}}))
        cmd = [sys.executable, str(HERE / "sana_eval.py"), "run", "--sana-os", str(so), "--variant", "before=v1",
               "--variant", "after=v2", "--cases", str(tmp / "cases.json"), "--config", str(tmp / "config.json"),
               "--label", "selftest", "--out", str(tmp / "runs")]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
        check(r.returncode == 0, "run exits 0" + ("" if r.returncode == 0 else f": {r.stderr[-500:]}"))
        runs = list((tmp / "runs").glob("*_selftest"))
        check(len(runs) == 1, "one run folder")
        if not runs:
            return 1
        run = runs[0]
        before = (run / "prompts/before/coaching.md").read_text(encoding="utf-8")
        after = (run / "prompts/after/coaching.md").read_text(encoding="utf-8")
        check(before == "Core A — 原則\n\nCore B\n\nCoaching v1", "composition: order, newlines stripped, one blank line")
        check(after.endswith("Coaching v2"), "the after variant reads the other commit")
        tmpl = (run / "prompts/after/t.md").read_text(encoding="utf-8")
        check(tmpl == "Core A — 原則\n\nFramework F\n\n# Template (v1.0)\n\n## 0. HEAD v2\nrule\n\n## 1. ROOT\nseq\n\n"
                      "## 2. CUSTOM\nmine\n\n## 3. UI\nmine too",
              "template: frameworks inserted, developer note removed, sections 2-3 replaced")
        c1 = [json.loads((run / f"transcripts/c1/{v}-{i}.json").read_text(encoding="utf-8"))
              for v in ("before", "after") for i in (1, 2)]
        check([x["seed"] for x in c1] == [101, 102, 101, 102], "repetition i has seed+i in every variant")
        second = [q["body"] for q in REQUESTS if len(q["body"]["messages"]) == 4]
        check(bool(second) and second[0]["messages"][2]["role"] == "assistant"
              and second[0]["messages"][2]["content"].startswith("echo"), "the reply is sent back in the next turn")
        check(all(q["auth"] == f"Bearer {SECRET}" for q in REQUESTS), "the key is sent to the endpoint")
        written = "".join(p.read_text(encoding="utf-8") for p in run.rglob("*") if p.is_file())
        check(SECRET not in written, "the key is not written to the run folder")
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        check("todo" in meta["skipped"] and not (run / "transcripts/todo").exists(), "a TODO case is skipped and listed")
        check(len(REQUESTS) == (2 + 1) * 2 * 2, "number of requests: (2 turns + 1 turn) x 2 reps x 2 variants")
        compare = (run / "compare.md").read_text(encoding="utf-8")
        check("### 2 回目 — after" in compare and "二つ目" in compare, "compare.md lists every repetition and turn")
        check((run / "review.md").is_file() and (run / "review.template.md").is_file(), "review.md and template written")
        check("**max_tokens で途中で切れています**" in compare and "t1/before-1" in compare, "a reply cut at max_tokens is flagged")
        check((run / "cases.snapshot.json").read_bytes() == (tmp / "cases.json").read_bytes(), "cases snapshot saved")

        # A filled-in review survives; a later edit of the cases file does not leak into reports.
        (run / "review.md").write_text("MY JUDGEMENT", encoding="utf-8")
        cases["cases"][0]["look_for"] = ["CHANGED LATER"]
        write(tmp / "cases.json", json.dumps(cases, ensure_ascii=False))
        rr = subprocess.run([sys.executable, str(HERE / "sana_eval.py"), "report", str(run)], capture_output=True, encoding="utf-8")
        check(rr.returncode == 0 and (run / "review.md").read_text(encoding="utf-8") == "MY JUDGEMENT",
              "report does not overwrite a filled-in review.md")
        check("CHANGED LATER" not in (run / "compare.md").read_text(encoding="utf-8"), "report uses the snapshot, not the edited cases")
        rr = subprocess.run([sys.executable, str(HERE / "sana_eval.py"), "report", str(run), "--cases", str(tmp / "cases.json")],
                            capture_output=True, encoding="utf-8")
        check(rr.returncode != 0, "a re-review without --reason is refused")
        rr = subprocess.run([sys.executable, str(HERE / "sana_eval.py"), "report", str(run), "--cases", str(tmp / "cases.json"),
                             "--reason", "new look-for points"], capture_output=True, encoding="utf-8")
        again = json.loads((run / "run.json").read_text(encoding="utf-8"))
        files = [p.name for p in run.glob("*.rereview-*")]
        copy = run / again.get("rereviews", [{}])[0].get("cases_copy", "missing")
        check(copy.is_file() and "CHANGED LATER" in copy.read_text(encoding="utf-8"), "a re-review keeps a copy of its cases")
        check("mock-model" in json.dumps(again.get("server", {})) and "/models の id：mock-model" in
              (run / "compare.md").read_text(encoding="utf-8"), "what the server says about itself is recorded")
        hdr = (run / "compare.md").read_text(encoding="utf-8").split("\n")
        at = {k: next((i for i, l in enumerate(hdr) if l.startswith(k)), -1) for k in ("- サーバー：", "- 比較するもの：", "  - **")}
        check(-1 not in at.values() and at["- サーバー："] < at["- 比較するもの："] < at["  - **"],
              "the server line does not split the list of variants")
        check(rr.returncode == 0 and again.get("rereviews", [{}])[0].get("reason") == "new look-for points"
              and any(f.startswith("compare.rereview-") for f in files)
              and (run / "review.md").read_text(encoding="utf-8") == "MY JUDGEMENT", "a re-review is recorded and kept apart")

        # A snapshot changed after the run is not used silently.
        snap = run / "cases.snapshot.json"
        snap.write_text(snap.read_text(encoding="utf-8").replace('"x"', '"EDITED"'), encoding="utf-8")
        rr = subprocess.run([sys.executable, str(HERE / "sana_eval.py"), "report", str(run)], capture_output=True, encoding="utf-8")
        check(rr.returncode != 0 and "changed after the run" in (rr.stderr + rr.stdout), "an edited snapshot is refused")

        # An unreachable endpoint stops the run at once.
        write(tmp / "dead.json", json.dumps({"base_url": "http://127.0.0.1:9/v1", "model": "x", "retries": 1, "timeout": 5}))
        dead = cmd[:]
        dead[dead.index("--config") + 1] = str(tmp / "dead.json")
        dead[dead.index("--label") + 1] = "dead"
        rd = subprocess.run(dead, capture_output=True, encoding="utf-8")
        druns = list((tmp / "runs").glob("*_dead"))
        meta = json.loads((druns[0] / "run.json").read_text(encoding="utf-8")) if druns else {}
        check(rd.returncode == 1 and "aborted" in meta and len(list(druns[0].rglob("transcripts/*/*.json"))) == 1,
              "an unreachable endpoint stops the run at the first request")
    server.shutdown()
    print("\n" + ("All checks passed." if not failures else f"{len(failures)} check(s) failed."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
