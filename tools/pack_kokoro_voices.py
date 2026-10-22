"""Собирает голоса Kokoro (*.bin из onnx-community) в один voices.npz для kokoro-onnx.

Каждый .bin — это float32-массив 510 x 1 x 256 (стиль голоса для каждой длины фраз).
Использование: python pack_kokoro_voices.py <папка с .bin> <выходной .npz>
"""

import sys
from pathlib import Path

import numpy as np


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    voices = {}
    for f in sorted(src.glob("*.bin")):
        arr = np.fromfile(f, dtype=np.float32)
        if arr.size % 256:
            print(f"пропускаю {f.name}: странный размер")
            continue
        voices[f.stem] = arr.reshape(-1, 1, 256)
    if not voices:
        print("не найдено ни одного голоса")
        return 1
    np.savez(dst, **voices)
    print(f"{dst.name}: {', '.join(voices)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
