#!/usr/bin/env python3
"""Print the words spoken in an audio file. The router runs it for each voice note, with the
Python of Whisper's own venv (voice_notes.WHISPER_PYTHON), so the router itself needs no packages.

  transcribe.py AUDIO MODELS_DIR

It runs only while a note is transcribed: the small model loads in about 2 seconds and takes
about a third of the note's length, on half the VM's cores. The language is detected."""
import os
import sys

from faster_whisper import WhisperModel

MODEL = "small"  # multilingual; large-v3-turbo hears better but took 3x as long here


def transcribe(audio, models_dir):
    threads = max(1, (os.cpu_count() or 2) // 2)  # leave the other half for everyone else
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=threads, download_root=models_dir)
    segments, _ = model.transcribe(audio, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__.split("\n\n")[1].strip())
    print(transcribe(sys.argv[1], sys.argv[2]))
