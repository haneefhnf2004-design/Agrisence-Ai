"""
Online/offline text brain: Gemini (cloud) when online and a key is set, otherwise the local
Ollama models (nexora:latest, qwen2.5:3b). Records which provider answered and why it fell back.
"""
import os

import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
ONLINE_MODEL = "gemini-2.5-flash"
OFFLINE_MODELS = ("nexora:latest", "qwen2.5:3b")


def ollama_models():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def status():
    from windows_control import internet_check
    net = internet_check()
    return {"online": net["online"], "gemini_key": bool(os.getenv("GEMINI_API_KEY")),
            "ollama_models": ollama_models()}


def _gemini(prompt, system):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    r = client.models.generate_content(model=ONLINE_MODEL, contents=prompt,
                                       config=types.GenerateContentConfig(system_instruction=system) if system else None)
    return (r.text or "").strip()


def _ollama(prompt, system, model):
    r = requests.post(f"{OLLAMA_URL}/api/generate", timeout=180,
                      json={"model": model, "prompt": prompt, "system": system or "", "stream": False})
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def ask(prompt, system=None, prefer="auto"):
    if not prompt or not str(prompt).strip():
        raise ValueError("prompt is required")
    errors = []
    if prefer in ("auto", "online") and os.getenv("GEMINI_API_KEY"):
        try:
            return {"answer": _gemini(prompt, system), "provider": "gemini", "model": ONLINE_MODEL}
        except Exception as e:
            errors.append(f"gemini: {type(e).__name__}: {str(e)[:120]}")
    if prefer in ("auto", "offline"):
        local = ollama_models()
        for m in OFFLINE_MODELS:
            if m in local:
                try:
                    return {"answer": _ollama(prompt, system, m), "provider": "ollama", "model": m,
                            "fallback_reasons": errors}
                except Exception as e:
                    errors.append(f"ollama {m}: {type(e).__name__}: {str(e)[:120]}")
        if not local:
            errors.append("ollama: not running or no models")
    raise RuntimeError("No AI provider answered. " + "; ".join(errors))
