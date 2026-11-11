"""Voice pipeline fixes: mic name matching, echo gate, interruption, mute, keep-alive (no network needed)."""
import asyncio
import os
import queue
import time

os.environ.setdefault("GEMINI_API_KEY", "test-key")  # ada builds its client at import time
import ada


def _loop():
    return ada.AudioLoop(video_mode="none")


def test_mangled_and_cut_off_mic_name_still_matches():
    saved = "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)"
    mme = "Microphone Array (IntelÂ® Smart "          # what Windows MME reported in the log
    want, have = ada._norm_device_name(saved), ada._norm_device_name(mme)
    assert want.startswith(have) and len(have) >= 12
    assert ada._norm_device_name("Headset (USB Audio)") not in want


def test_nexora_counts_as_speaking_until_the_speaker_finishes():
    a = _loop()
    assert not a._model_audio_active()
    a._play_busy_until = time.monotonic() + 1.0     # speaker still playing, nothing new arriving
    assert a._model_audio_active()
    a._play_busy_until = 0.0
    a._playback_q = queue.Queue()
    a._playback_q.put(b"\x00" * 10)                   # audio still waiting to play
    assert a._model_audio_active()


def test_interruption_drops_audio_in_both_queues():
    async def run():
        a = _loop()
        a.audio_in_queue = asyncio.Queue()
        a._playback_q = queue.Queue()
        for _ in range(3):
            a.audio_in_queue.put_nowait(b"x")
            a._playback_q.put(b"y")
        a._play_busy_until = time.monotonic() + 5
        a.clear_audio_queue()
        assert a.audio_in_queue.empty() and a._playback_q.empty()
        assert not a._model_audio_active()
    asyncio.run(run())


def test_muting_mid_sentence_clears_user_speaking():
    a = _loop()
    a.signals.mic_open = True
    a.set_paused(True)
    assert a.signals.mic_open is False


def test_silence_keepalive_is_sent_when_nothing_else_is(monkeypatch):
    monkeypatch.setattr(ada, "KEEPALIVE_SECONDS", 0.05)
    sent = []

    class FakeSession:
        async def send_realtime_input(self, **kw):
            sent.append(kw)

    async def run():
        a = _loop()
        a.session = FakeSession()
        a.out_queue = asyncio.Queue()
        task = asyncio.create_task(a.send_realtime())
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(run())
    assert sent and all(k["audio"]["data"] == ada.SILENT_CHUNK for k in sent)
