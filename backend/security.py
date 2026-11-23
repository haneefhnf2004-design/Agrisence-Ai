"""
Security helpers shared by every tool.

- contains_secret / redact: spot passwords, API keys and tokens so they are never saved as
  memories, notes or tasks, and never written to the audit log.
- audit(): append-only JSONL log of every tool call (who asked, which tool, redacted args, result).
- needs_confirmation(): which tool calls must wait for the user's on-screen APPROVE click.
"""
import json
import os
import re
import threading
import time

AUDIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")
_MAX_AUDIT_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                      # Google API key
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),                      # OpenAI/Anthropic style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),                  # GitHub token
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),               # Slack token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|pwd|passcode|pin|api[_ -]?key|secret|token|otp)\b\s*(is|=|:)\s*\S+"),
    re.compile(r"\b\d{13,19}\b"),                                 # card-number-like digit runs
]


def contains_secret(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact(value):
    """Return a copy of value with secret-looking substrings replaced by [REDACTED]."""
    if isinstance(value, str):
        out = value
        for p in _SECRET_PATTERNS:
            out = p.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)pass|secret|token|key", str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def audit(tool: str, source: str, args=None, ok=True, detail: str = ""):
    """Append one record. Never raises: auditing must not break the tool."""
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "source": source,
           "args": redact(args or {}), "ok": bool(ok), "detail": redact(str(detail))[:300]}
    try:
        with _lock:
            if os.path.exists(AUDIT_PATH) and os.path.getsize(AUDIT_PATH) > _MAX_AUDIT_BYTES:
                os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def recent_audit(limit=50, tool=None):
    if not os.path.exists(AUDIT_PATH):
        return []
    with open(AUDIT_PATH, encoding="utf-8") as f:
        lines = f.readlines()[-2000:]
    rows = []
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if tool and r.get("tool") != tool:
            continue
        rows.append(r)
        if len(rows) >= limit:
            break
    return rows


# tool name -> set of `action` values that need the user's approval click (None = every call).
SENSITIVE = {
    "system_control": {"shutdown", "restart", "sleep", "kill_process", "close_app"},
    "files": {"organize_apply"},
    "manage_memory": {"delete", "clear_all", "import"},
    "productivity": set(),
}


def needs_confirmation(name: str, args: dict) -> bool:
    acts = SENSITIVE.get(name)
    if acts is None:
        return False
    return str((args or {}).get("action", "")).lower() in acts
