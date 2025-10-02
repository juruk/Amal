import os, json, time, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STATUS = DOCS / "status.json"

def load_event():
    p = os.environ.get("GITHUB_EVENT_PATH")
    if not p or not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    ev = load_event()
    issue_no = None
    title = ""
    if "issue" in ev:
        issue_no = ev["issue"]["number"]
        title = ev["issue"].get("title","")
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": "smoke-orchestrator",
        "ts": int(time.time()),
        "issue": issue_no,
        "title": title,
        "progress": 42
    }
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[Amal] Orchestrator wrote docs/status.json:")
    print(STATUS.read_text(encoding="utf-8"))

if __name__ == "__main__":
    main()
