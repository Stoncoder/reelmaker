"""Chatterbox voice worker. Runs in its own environment (.venv-tts) and stays loaded.

Reads one JSON request per line on stdin:
  {"text": "...", "out": "/path/out.wav", "ref": "/path/sample.wav" | null, "exaggeration": 0.65, "cfg": 0.4}
Writes one JSON line per request on stdout: {"ok": true, "seconds": 2.4} or {"ok": false, "error": "..."}
"""
import json
import sys

OUT = sys.stdout          # our replies only
sys.stdout = sys.stderr   # anything the libraries print goes to stderr

import torch
import torchaudio

# Apple GPU on Macs, NVIDIA GPU on PCs that have one, otherwise the CPU (slower, but works)
device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
_orig_load = torch.load


def _load(*a, **k):  # checkpoints were saved on CUDA; load them onto this Mac's device
    k.setdefault("map_location", torch.device(device))
    return _orig_load(*a, **k)


torch.load = _load

from chatterbox.tts import ChatterboxTTS  # noqa: E402

model = ChatterboxTTS.from_pretrained(device=device)
print(json.dumps({"ready": True, "device": device}), file=OUT, flush=True)

for line in sys.stdin:
    try:
        req = json.loads(line)
        wav = model.generate(req["text"], audio_prompt_path=req.get("ref") or None,
                             exaggeration=float(req.get("exaggeration", 0.65)), cfg_weight=float(req.get("cfg", 0.4)))
        torchaudio.save(req["out"], wav.cpu(), model.sr)
        print(json.dumps({"ok": True, "seconds": wav.shape[-1] / model.sr}), file=OUT, flush=True)
    except Exception as e:  # keep serving
        print(json.dumps({"ok": False, "error": str(e)[:500]}), file=OUT, flush=True)
