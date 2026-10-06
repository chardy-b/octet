"""Write PCM WAV files that streamers accept (16/24-bit, which scipy can't write)."""
import wave

import numpy as np


def write_pcm_wav(path, fs: int, y: np.ndarray, bits: int = 24, channels: int = 2) -> None:
    y = np.clip(np.asarray(y, dtype=np.float64), -1.0, 1.0)
    frames = np.repeat(y[:, None], channels, axis=1) if y.ndim == 1 else y
    if bits == 16:
        raw = (frames * 32767).astype("<i2").tobytes()
    elif bits == 24:
        v = (frames * 8388607).astype("<i4")
        raw = v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    elif bits == 32:
        raw = (frames * 2147483647).astype("<i4").tobytes()
    else:
        raise ValueError("bits must be 16, 24 or 32")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(frames.shape[1])
        w.setsampwidth(bits // 8)
        w.setframerate(fs)
        w.writeframes(raw)
