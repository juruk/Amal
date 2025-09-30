# C:\Amal\.amal\orchestrator.py
import os, json, time, re, subprocess, textwrap, pathlib, sys
from jsonschema import validate
import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS_STATUS = ROOT / "docs" / "status.json"

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
    event_path = os.environ.get("GH_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return {}
    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)

def gh_api(path, method="GET", json_body=None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GH_REPO")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}"
    url = f"{base}{path}"
    resp = requests.request(method, url, json=json_body, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()

def post_issue_comment(number: int, body: str):
    gh_api(f"/issues/{number}/comments", method="POST", json_body={"body": body})

def update_status(data: dict):
    # merge/overwrite
    existing = {}
    if DOCS_STATUS.exists():
        try:
            existing = json.loads(DOCS_STATUS.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.update(data)
    DOCS_STATUS.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

def detect_issue_context(ev: dict):
    if ev.get("issue"):
        issue = ev["issue"]
        number = issue["number"]
        body = issue.get("body") or ""
        title = issue.get("title") or ""
        return number, title, body
    # workflow_dispatch -> read last opened "amal:run" labeled issue?
    return None, "", ""

def parse_acceptance(issue_body: str):
    """
    Поддржува YAML блок ваков:
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

def run_cmd(cmd, cwd=None, timeout=120):
    start = time.time()
    p = subprocess.Popen(cmd, cwd=cwd or ROOT, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
    if not patch_text.strip():
        return True, "No patch provided"
    # Write temp patch
    tmp = ROOT / ".amal" / "tmp.patch"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(patch_text, encoding="utf-8")

    # Use python-patch CLI (works cross-platform)
    # pip installed 'python-patch' exposes 'patch' module, but CLI is python -m patch
    code, out, _ = run_cmd(f'python -m patch -p0 -i "{tmp.as_posix()}"', cwd=ROOT.as_posix(), timeout=60)
    if code != 0:
        return False, f"Patch failed (code {code}). Output:\n{out}"
    return True, "Patch applied"

def call_model(system_prompt: str, user_prompt: str) -> dict:
    provider = os.environ.get("AMAL_PROVIDER", "ollama")
    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        url = f"{base}/api/chat"
        payload = {
            "model": "llama3.1",
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
        # try parse JSON
        try:
            return json.loads(content)
        except Exception:
            # Try to extract fenced JSON
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            raise
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

def build_system_prompt():
    return textwrap.dedent("""
    You are a rigorous coding agent. You MUST output a SINGLE JSON object with fields:
      - "patch": (optional) a valid unified diff
      - "commands": (array) objects with fields { "name"?:string, "cmd":string, "cwd"?:string, "timeout"?:int }
    No explanations. No prose. JSON only.
    If you need to modify files, include a unified diff in "patch".
    Keep commands idempotent where possible. Prefer Windows-friendly commands.
    """)

def build_user_prompt(issue_title: str, issue_body: str, logs: str, iteration: int, files_listing: str, acc_desc: str):
    return textwrap.dedent(f"""
    Task Title: {issue_title}

    Task Body:
    {issue_body}

    Iteration: {iteration}

    Repo files (truncated):
    {files_listing}

    Last logs (truncated):
    {logs}

    Acceptance (summary):
    {acc_desc}

    Produce STRICT JSON per schema. Avoid destructive commands.
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
    if not issue_no:
        print("No issue context; run via workflow_dispatch or issues:labeled")
    acceptance = parse_acceptance(issue_body or "")
    acc_desc = json.dumps(acceptance, ensure_ascii=False) if acceptance else "None"

    update_status({"phase": "start", "issue": issue_no, "ts": int(time.time())})
    if issue_no:
        post_issue_comment(issue_no, f"🚀 **Amal** стартуваше. Provider=Ollama. Acceptance={acc_desc}")

    system_prompt = build_system_prompt()
    iteration = 1
    max_iter = 6
    last_logs = ""
    files_list = list_files()

    while iteration <= max_iter:
        update_status({"phase": "iterating", "iteration": iteration, "progress": int((iteration-1)/max_iter*100)})
        user_prompt = build_user_prompt(issue_title, issue_body, last_logs[-2000:], iteration, files_list, acc_desc)
        try:
            instr = call_model(system_prompt, user_prompt)
            validate(instr, INSTRUCTION_SCHEMA)
        except Exception as e:
            msg = f"❌ Invalid model output on iter {iteration}: {e}"
            if issue_no: post_issue_comment(issue_no, msg)
            update_status({"error": msg})
            break

        # Apply patch
        patch_msg = "No patch"
        if "patch" in instr and instr["patch"]:
            ok, patch_msg = apply_patch(instr["patch"])
            if not ok:
                last_logs = patch_msg
                if issue_no: post_issue_comment(issue_no, f"❌ Patch error:\n```\n{patch_msg}\n```")
                update_status({"error": "patch_failed", "detail": patch_msg})
                iteration += 1
                continue

        # Run commands
        run_logs = []
        for i, c in enumerate(instr.get("commands", []), start=1):
            cmd = c["cmd"]
            cwd = c.get("cwd", str(ROOT))
            timeout = c.get("timeout", 180)
            code, out, dur = run_cmd(cmd, cwd=cwd, timeout=timeout)
            run_logs.append(f"$ {cmd}\n# exit={code}, {dur}s\n{out}")
            if issue_no:
                post_issue_comment(issue_no, f"🔧 Команда {i}: `{cmd}` → exit={code}\n```\n{out[:5000]}\n```")
            if code != 0:
                # fail fast; send back to model next loop
                break

        # Save logs
        last_logs = patch_msg + "\n\n" + "\n\n---\n\n".join(run_logs)
        update_status({"last_logs": last_logs[-4000:]})

        # Acceptance
        if acceptance:
            code, out, _ = run_cmd(acceptance["cmd"], cwd=str(ROOT), timeout=int(acceptance.get("timeout", 60)))
            passed = (code == 0 and acceptance["expect_contains"] in out)
            if issue_no:
                post_issue_comment(issue_no, f"🧪 Acceptance run: exit={code}\n```\n{out[:4000]}\n```")
            if passed:
                update_status({"phase": "done", "progress": 100, "result": "passed"})
                if issue_no: post_issue_comment(issue_no, "✅ **Acceptance PASSED**. Задачата е готова.")
                return
            else:
                if issue_no: post_issue_comment(issue_no, "⚠️ Acceptance NOT met. Ќе итерирам понатаму.")
        else:
            # ако нема Acceptance, заврши по прва успешна итерација
            update_status({"phase": "done_no_accept", "progress": 100})
            if issue_no: post_issue_comment(issue_no, "ℹ️ Нема дефинирано ACCEPT. Завршив итерација.")
            return

        iteration += 1
        time.sleep(2)

    # max iterations reached
    update_status({"phase": "failed", "result": "max_iterations"})
    if issue_no:
        post_issue_comment(issue_no, "❌ Достигнат максимум итерации без PASS.")
    sys.exit(1)

if __name__ == "__main__":
    main()
