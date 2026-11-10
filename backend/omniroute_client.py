"""Hand big jobs (assignments, programs, reports) to OmniRoute, which routes across every connected model.

OmniRoute speaks the OpenAI API at OMNIROUTE_URL (default http://localhost:20128/v1). Model 'auto' lets its
router pick the best healthy model and fall back automatically. Files the model writes are saved under
~/Desktop/Nexora Builds/<task>/.
"""
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

import requests

URL = os.getenv("OMNIROUTE_URL", "http://localhost:20128/v1").rstrip("/")
MODEL = os.getenv("OMNIROUTE_MODEL", "auto")
BUILD_ROOT = Path(os.getenv("NEXORA_BUILD_DIR", Path.home() / "Desktop" / "Nexora Builds"))

SYSTEM = (
    "You are Nexora's build engine. Complete the user's task fully: working code, full assignment answers, "
    "reports or study material. Explain your approach briefly, then output EVERY deliverable file in this exact format:\n"
    "=== FILE: relative/path.ext ===\n<full file content>\n=== END FILE ===\n"
    "Use relative paths only. Include a README.md saying how to run or use the work. Never leave TODOs or placeholders."
)
_FILE_RE = re.compile(r"^=== FILE: (.+?) ===\r?\n(.*?)^=== END FILE ===", re.S | re.M)


def _headers():
    key = os.getenv("OMNIROUTE_API_KEY")
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}


def safe_relpath(path):
    """The model's file path as a safe relative path, or None if it tries to escape the build folder."""
    p = PurePosixPath(path.strip().replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts or ":" in p.parts[0]:
        return None
    return Path(*p.parts)


def parse_files(text):
    """[(relative Path, content)] from the model's answer. Unsafe paths are dropped."""
    out = []
    for raw_path, content in _FILE_RE.findall(text or ""):
        rel = safe_relpath(raw_path)
        if rel is not None:
            out.append((rel, content.rstrip("\n") + "\n"))
    return out


def _slug(text):
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task")


def run_task(task, model=None, context=""):
    """Blocking. Returns {'answer', 'model', 'folder', 'files'} or {'error'}."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": task + (f"\n\nExtra context:\n{context}" if context else "")}]
    try:
        r = requests.post(f"{URL}/chat/completions", headers=_headers(), timeout=600,
                          json={"model": model or MODEL, "messages": messages})
        data = r.json()
    except requests.ConnectionError:
        return {"error": "OmniRoute is not running. Start it with the 'omniroute' command (port 20128)."}
    except Exception as e:
        return {"error": f"OmniRoute request failed: {e}"}
    if "error" in data:
        msg = data["error"].get("message", str(data["error"])) if isinstance(data["error"], dict) else str(data["error"])
        return {"error": f"OmniRoute could not complete it: {msg[:400]}. Check your providers at http://localhost:20128."}
    answer = data["choices"][0]["message"].get("content") or ""
    files = parse_files(answer)
    folder = BUILD_ROOT / f"{datetime.now():%Y%m%d-%H%M}-{_slug(task)}"
    folder.mkdir(parents=True, exist_ok=True)
    for rel, content in files:
        dest = folder / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    (folder / "NEXORA_RESPONSE.md").write_text(f"# Task\n\n{task}\n\n# Response ({data.get('model', model or MODEL)})\n\n{answer}\n",
                                               encoding="utf-8")
    return {"answer": _FILE_RE.sub("", answer).strip(), "model": data.get("model", model or MODEL),
            "folder": str(folder), "files": [str(rel).replace("\\", "/") for rel, _ in files]}


if __name__ == "__main__":  # self-check of the parser and the path guard
    sample = ("Plan.\n=== FILE: src/app.py ===\nprint('hi')\n=== END FILE ===\n"
              "=== FILE: ../evil.txt ===\nx\n=== END FILE ===\n=== FILE: C:/Windows/x.txt ===\nx\n=== END FILE ===\n")
    got = parse_files(sample)
    assert [(str(p).replace("\\", "/"), c) for p, c in got] == [("src/app.py", "print('hi')\n")], got
    assert safe_relpath("/etc/passwd") is None and safe_relpath("a/../../b") is None and safe_relpath("ok/f.txt")
    print("omniroute_client self-check OK")
