import os, json, time, pathlib, re, subprocess, textwrap
from jsonschema import validate
import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATUS = DOCS / "status.json"

INSTRUCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cmd": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"}
                },
                "required": ["cmd"]
            }
        }
    },
    "required": []
}

def load_event():
    # GitHub ја дава патеката во оваа env променлива
    p = os.environ.get("GITHUB_EVENT_PATH")
    if not p or not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def gh_api(path, method="GET", json_body=None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GH_REPO")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY missing")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo}"
    url = f"{base}{path}"
    r = requests.request(method, url, json=json_body, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

def post_issue_comment(number: int, body: str):
    try:
        gh_api(f"/issues/{number}/comments", method="POST", json_body={"body": body})
    except Exception as e:
        print(f"[warn] post_issue_comment failed: {e}")

def update_status(data: dict):
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = {}
    if STATUS.exists():
        try:
            payload = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload.update(data)
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def detect_issue_context(ev: dict):
    if ev.get("issue"):
        issue = ev["issue"]
        return issue["number"], issue.get("title",""), issue.get("body","")
    return None, "", ""

def parse_acceptance(issue_body: str):
    """
    Очекуваме во Issue тело fenced YAML со клуч ACCEPT:
    ```yaml
    ACCEPT:
      cmd: python sample_app/app.py
      expect_contains: Hello, Phase 8!
      timeout: 30
    ```
    """
    m = re.search(r"```yaml\s*(.*?)```", issue_body, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    try:
        doc = yaml.safe_load(m.group(1))
        acc = doc.get("ACCEPT")
        if isinstance(acc, dict) and "cmd" in acc and "expect_contains" in acc:
            return acc
    except Exception:
        return None
    return None

def run_cmd(cmd, cwd=None, timeout=180):
    start = time.time()
    p = subprocess.Popen(cmd, cwd=cwd or str(ROOT), shell=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out = f"[TIMEOUT after {timeout}s]"
        code = -9
    dur = round(time.time() - start, 2)
    return code, out, dur

def apply_patch(patch_text: str):
    if not patch_text or not patch_text.strip():
        return True, "No patch provided"
    tmp = ROOT / ".amal" / "tmp.patch"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(patch_text, encoding="utf-8")
    # git apply е достапен (runner има git). -p0 бидејќи ќе даваме целосни патеки во diff-овите
    code, out, _ = run_cmd(f'git apply -p0 --whitespace=nowarn -v "{tmp.as_posix()}"', cwd=str(ROOT), timeout=90)
    if code != 0:
        return False, f"git apply failed (code {code}). Output:\n{out}"
    return True, "Patch applied"

def call_ollama(system_prompt: str, user_prompt: str) -> dict:
    base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    model = os.environ.get("AMAL_MODEL", "llama3.1")
    url = f"{base}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.2}
    }
    r = requests.post(url, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    content = data.get("message", {}).get("content", "")
    # Очекуваме JSON. Ако има текст околу него, извлечи го првиот JSON објект.
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("Model did not return JSON")

def build_system_prompt():
    return textwrap.dedent("""
    You are a strict coding agent. Output a SINGLE JSON object with fields:
      - "patch": (optional) unified diff to apply at repo root
      - "commands": array of { "name"?:string, "cmd":string, "cwd"?:string, "timeout"?:int }
    No explanations. JSON only.
    Prefer idempotent, Windows-friendly commands. Avoid destructive actions.
    """)

def list_files(max_len=2000):
    items = []
    for p in ROOT.rglob("*"):
        if any(part.startswith(".git") for part in p.parts):
            continue
        if p.is_file():
            rel = p.relative_to(ROOT).as_posix()
            items.append(rel)
    s = "\n".join(items)
    return s[:max_len]

def main():
    ev = load_event()
    issue_no, issue_title, issue_body = detect_issue_context(ev)
    acceptance = parse_acceptance(issue_body or "")
    acc_desc = json.dumps(acceptance, ensure_ascii=False) if acceptance else "None"

    update_status({"phase": "start", "issue": issue_no, "title": issue_title, "ts": int(time.time()), "progress": 0})
    if issue_no:
        post_issue_comment(issue_no, f"🚀 Amal стартува. Acceptance={acc_desc}")

    system_prompt = build_system_prompt()
    iteration = 1
    max_iter = 4
    last_logs = ""
    files_list = list_files()

    while iteration <= max_iter:
        update_status({"phase": "iterating", "iteration": iteration, "progress": int((iteration-1)/max_iter*100)})

        user_prompt = textwrap.dedent(f"""
        Task Title: {issue_title}

        Task Body:
        {issue_body}

        Iteration: {iteration}

        Repo files (truncated):
        {files_list}

        Last logs (truncated):
        {last_logs[-1200:]}

        Acceptance (summary):
        {acc_desc}
        """)

        try:
            instr = call_ollama(system_prompt, user_prompt)
            validate(instr, INSTRUCTION_SCHEMA)
        except Exception as e:
            msg = f"❌ Invalid model output on iter {iteration}: {e}"
            if issue_no: post_issue_comment(issue_no, msg)
            update_status({"error": msg})
            break

        # Apply patch ако има
        patch_msg = "No patch"
        if instr.get("patch"):
            ok, patch_msg = apply_patch(instr["patch"])
            if issue_no:
                post_issue_comment(issue_no, f"🩹 Patch: {('OK' if ok else 'FAIL')}\n```\n{patch_msg[:4000]}\n```")
            if not ok:
                last_logs = patch_msg
                iteration += 1
                continue

        # Изврши команди
        run_logs = []
        cmds = instr.get("commands", [])
        for i, c in enumerate(cmds, start=1):
            cmd = c["cmd"]
            cwd = c.get("cwd", str(ROOT))
            timeout = int(c.get("timeout", 180))
            code, out, dur = run_cmd(cmd, cwd=cwd, timeout=timeout)
            run_logs.append(f"$ {cmd}\n# exit={code}, {dur}s\n{out}")
            if issue_no:
                post_issue_comment(issue_no, f"🔧 Команда {i}: `{cmd}` → exit={code}\n```\n{out[:3000]}\n```")
            if code != 0:
                break

        last_logs = patch_msg + "\n\n" + "\n\n---\n\n".join(run_logs)
        update_status({"last_logs": last_logs[-3500:]})

        # Acceptance
        if acceptance:
            code, out, _ = run_cmd(acceptance["cmd"], cwd=str(ROOT), timeout=int(acceptance.get("timeout", 60)))
            passed = (code == 0 and acceptance["expect_contains"] in out)
            if issue_no:
                post_issue_comment(issue_no, f"🧪 Acceptance: exit={code}\n```\n{out[:3000]}\n```")
            if passed:
                update_status({"phase": "done", "progress": 100, "result": "passed"})
                if issue_no: post_issue_comment(issue_no, "✅ **Acceptance PASSED**. Задачата е готова.")
                print("[Amal] DONE (acceptance passed)")
                return
            else:
                if issue_no: post_issue_comment(issue_no, "⚠️ Acceptance NOT met. Итерација понатаму.")
        else:
            update_status({"phase": "done_no_accept", "progress": 100})
            if issue_no: post_issue_comment(issue_no, "ℹ️ Нема ACCEPT. Завршив една итерација.")
            print("[Amal] DONE (no acceptance defined)")
            return

        iteration += 1
        time.sleep(2)

    update_status({"phase": "failed", "result": "max_iterations"})
    if issue_no:
        post_issue_comment(issue_no, "❌ Достигнат максимум итерации без PASS.")
    print("[Amal] FAIL (max iterations)")
    raise SystemExit(1)

if __name__ == "__main__":
    main()
