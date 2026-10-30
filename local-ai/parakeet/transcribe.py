"""Transcribe a 16 kHz mono wav with Parakeet TDT v3 (sherpa-onnx); print the text."""
import sys, wave
from pathlib import Path
import numpy as np
import sherpa_onnx

M = Path(__file__).parent / "model-v2"  # English-only Parakeet; multilingual v3 is in model/

def load():
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(M / "encoder.int8.onnx"),
        decoder=str(M / "decoder.int8.onnx"),
        joiner=str(M / "joiner.int8.onnx"),
        tokens=str(M / "tokens.txt"),
        model_type="nemo_transducer",
        num_threads=8,
    )

def transcribe(rec, path):
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    s = rec.create_stream()
    s.accept_waveform(sr, a)
    rec.decode_stream(s)
    return s.result.text.strip()

if __name__ == "__main__":
    rec = load()
    for p in sys.argv[1:]:
        print(transcribe(rec, p))
