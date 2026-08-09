"""Turn text into PCM the device can play.

Synthesis happens here rather than on the device because it has to: the
Cardputer-Adv is an ESP32-S3, and the only M5Stack path to on-device
Japanese TTS is the Module LLM (an AX630C running Linux and MeloTTS),
which is a separate board this project does not have. Espressif's own
`esp-tts` is Chinese-only. So the Mac speaks and the device plays —
which fits the architecture anyway, since the Mac is on the other end
of the USB cable by definition here.

macOS `say` does the whole job: it takes a voice, a sample rate and an
output format, so there is no second conversion step.

### Sandbox

`say` produces a 4096-byte header-and-padding file with no audio and
exit status 0 when it cannot reach the speech synthesis service, which
is what happens inside the Claude Code sandbox. Running under `uv run`
puts this outside it — the same escape the serial port needs, see
CLAUDE.md. `synthesize` treats a missing or empty data chunk as an
error rather than passing silence down the wire, because the two
failures look identical from the device end.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

# 16 kHz 16-bit mono. The device consumes 32 KB/s at this rate against a
# measured 182 KiB/s link, so there is no reason to trade quality for
# bandwidth. `say` writes LEI16 natively, which is exactly what
# M5.Speaker.playRaw expects.
DEFAULT_RATE = 16000
DEFAULT_VOICE = "Kyoko"

# A `say` that cannot synthesise still writes a header and padding.
# Anything at or under this is that file, not audio.
_EMPTY_WAV_BYTES = 4096


class SynthesisError(RuntimeError):
    """`say` produced no audio."""


def available() -> bool:
    """Whether `say` is on this machine at all."""
    return shutil.which("say") is not None


def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: int = DEFAULT_RATE,
) -> bytes:
    """Return `text` as signed 16-bit little-endian mono PCM.

    No WAV header, no padding: just the samples, ready to hand to
    `playRaw`.
    """
    if not text.strip():
        raise SynthesisError("nothing to say")
    if not available():
        raise SynthesisError("`say` not found — this path needs macOS")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "speech.wav"
        proc = subprocess.run(
            [
                "say",
                "-v",
                voice,
                f"--data-format=LEI16@{rate}",
                "--file-format=WAVE",
                "-o",
                str(out),
                text,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SynthesisError(f"say failed ({proc.returncode}): {proc.stderr.strip()}")
        if not out.exists():
            raise SynthesisError("say wrote no file")
        if out.stat().st_size <= _EMPTY_WAV_BYTES:
            raise SynthesisError(
                "say produced a header with no audio — the speech synthesis "
                "service is unreachable. Run this under `uv run` so it is "
                "outside the sandbox."
            )
        return _pcm_from_wav(out)


def _pcm_from_wav(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SynthesisError(
                f"expected 16-bit mono, got {w.getnchannels()}ch/{w.getsampwidth() * 8}-bit"
            )
        frames = w.readframes(w.getnframes())
    if not frames:
        # `say` writes a streaming header whose frame count can read as
        # zero; readframes still returns the data when there is any.
        raise SynthesisError("wav file contained no samples")
    return frames


def duration_s(pcm: bytes, rate: int = DEFAULT_RATE) -> float:
    """How long `pcm` takes to play, in seconds."""
    return len(pcm) / 2 / rate
