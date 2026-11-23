"""
Wake word listener for NEXORA.
Listens in the background for trigger phrases like "Hey NEXORA"
and fires a callback when detected.
"""

import threading
import time

try:
    import speech_recognition as sr
    _SR_AVAILABLE = True
except ImportError:
    _SR_AVAILABLE = False
    print("[WAKE] speech_recognition not installed – wake word disabled.")

WAKE_PHRASES = ["hey nexora", "hi nexora", "hey nexus", "hey nikora"]
COOLDOWN_SECONDS = 3


class WakeWordListener:
    def __init__(self, on_wake=None):
        self.on_wake = on_wake
        self._stop_event = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ #
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ #
    def start(self):
        """Start the background daemon thread."""
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="WakeWordListener")
        self._thread.start()

    def stop(self, wait=False):
        """Signal the background thread to stop. wait=True blocks (max ~5s) until it has released the mic,
        so PortAudio isn't shut down on this thread while the voice loop opens it on another."""
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if wait and thread is not None and thread.is_alive():
            thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    def _listen_loop(self):
        if not _SR_AVAILABLE:
            return

        try:
            recognizer = sr.Recognizer()
            recognizer.energy_threshold = 300
            recognizer.dynamic_energy_threshold = True

            with sr.Microphone() as source:
                # Adjust for ambient noise once at startup
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=1)
                except Exception:
                    pass

                while not self._stop_event.is_set():
                    try:
                        audio = recognizer.listen(source, timeout=1, phrase_time_limit=2)
                    except sr.WaitTimeoutError:
                        continue
                    except Exception:
                        continue

                    if self._stop_event.is_set():
                        break

                    try:
                        text = recognizer.recognize_google(audio).lower()
                        if any(phrase in text for phrase in WAKE_PHRASES):
                            print(f"[WAKE] Detected: '{text}'")
                            if callable(self.on_wake):
                                self.on_wake()
                            # Cooldown: ignore further detections for a bit
                            self._stop_event.wait(COOLDOWN_SECONDS)
                    except Exception:
                        pass  # sr.UnknownValueError, sr.RequestError, network issues, etc.

        except Exception:
            pass  # Never crash the background thread under any circumstances


# Module-level singleton
wake_listener = WakeWordListener()
