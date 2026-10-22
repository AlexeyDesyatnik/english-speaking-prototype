"""Бенчмарк офлайн-TTS (Kokoro и Piper) для школьных ноутбуков.

Запуск: run_bench.bat (или runtime\\python.exe bench\\bench.py).

Каждый движок гоняется в отдельном процессе: так честно меряется память
и падение одного движка (например, нехватка RAM) не роняет весь тест.
Результаты складываются в results\\<имя компьютера>\\, поэтому с одной
флешки можно прогнать оба ноутбука, ничего не перезаписав.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
RESULTS = ROOT / "results"

BENCH_TEXT = (
    "Good morning, everyone. Today we are going to read a short story about a girl named Emma. "
    "Emma lives in a small town near the sea. Every Saturday, she walks to the market with her grandmother. "
    "They buy fresh fish, warm bread and a bag of green apples. "
    "Emma likes the market because it is always busy and a little bit noisy. "
    "\"What would you like for dinner?\" asks her grandmother. "
    "\"Pancakes, please!\" Emma answers with a big smile. "
    "Our next lesson starts at 8:30 on Monday, the 3rd of November."
)

SAMPLE_TEXT = (
    "Hello! My name is Emma, and I am eleven years old. "
    "I live in a small town near the sea, and I love reading books about animals."
)

# Движки для замера скорости: имя -> описание
ENGINES = {
    "kokoro-fp32": {"kind": "kokoro", "model": "kokoro/kokoro-fp32.onnx", "voice": "af_heart",
                    "title": "Kokoro (полная модель, fp32)"},
    "kokoro-int8": {"kind": "kokoro", "model": "kokoro/kokoro-int8.onnx", "voice": "af_heart",
                    "title": "Kokoro (сжатая модель, int8)"},
    "piper-medium": {"kind": "piper", "model": "piper/en_US-lessac-medium.onnx",
                     "title": "Piper lessac, качество medium"},
    "piper-high": {"kind": "piper", "model": "piper/en_US-lessac-high.onnx",
                   "title": "Piper lessac, качество high"},
}

# Голоса для прослушивания (режим --samples)
KOKORO_SAMPLE_VOICES = {
    "af_heart": "US, женский",
    "af_bella": "US, женский",
    "am_michael": "US, мужской",
    "bf_emma": "UK, женский",
    "bm_george": "UK, мужской",
}
PIPER_SAMPLE_VOICES = {
    "en_US-lessac-medium": "US, женский",
    "en_US-ryan-medium": "US, мужской",
    "en_GB-cori-medium": "UK, женский",
}


# ---------------------------------------------------------------- система

def cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "неизвестно"


def total_ram_gb() -> float:
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / 2**30
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass
    return 0.0


def peak_rss_mb() -> float:
    """Пиковая память текущего процесса."""
    if sys.platform == "win32":
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 2**20
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def system_info() -> dict:
    return {
        "computer": platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "cpu": cpu_name(),
        "logical_cpus": os.cpu_count(),
        "ram_gb": round(total_ram_gb(), 1),
        "python": platform.python_version(),
    }


# ---------------------------------------------------------------- текст и звук

def split_sentences(text: str) -> list[str]:
    """Простое деление на предложения: по . ! ? с учётом кавычек."""
    parts = re.findall(r'.+?(?:[.!?]+["”]?(?=\s|$)|$)', text.strip(), flags=re.S)
    return [p.strip() for p in parts if p.strip()]


def write_wav(path: Path, samples, sample_rate: int) -> None:
    """samples: numpy float32 [-1..1] или int16."""
    import numpy as np
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        arr = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(arr.tobytes())


# ---------------------------------------------------------------- движки

class KokoroEngine:
    def __init__(self, model_path: Path):
        from kokoro_onnx import Kokoro
        self.tts = Kokoro(str(model_path), str(MODELS / "kokoro" / "voices.npz"))
        self.sample_rate = 24000

    def synth(self, text: str, voice: str, speed: float = 1.0):
        audio, sr = self.tts.create(text, voice=voice, speed=speed, lang="en-us" if voice[0] == "a" else "en-gb")
        self.sample_rate = sr
        return audio


class PiperEngine:
    def __init__(self, model_path: Path):
        from piper import PiperVoice
        self.voice = PiperVoice.load(str(model_path))
        self.sample_rate = self.voice.config.sample_rate

    def synth(self, text: str, voice: str | None = None, speed: float = 1.0):
        import numpy as np
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=1.0 / speed)
        chunks = [c.audio_int16_array for c in self.voice.synthesize(text, syn_config=cfg)]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)


def make_engine(kind: str, model_path: Path):
    return KokoroEngine(model_path) if kind == "kokoro" else PiperEngine(model_path)


# ---------------------------------------------------------------- рабочий процесс

def worker_bench(name: str, out_dir: Path) -> dict:
    """Замер одного движка. Вызывается в отдельном процессе."""
    import numpy as np
    spec = ENGINES[name]
    model_path = MODELS / spec["model"]
    if not model_path.exists():
        return {"engine": name, "error": f"нет файла модели {model_path.relative_to(ROOT)}"}

    t0 = time.perf_counter()
    engine = make_engine(spec["kind"], model_path)
    load_s = time.perf_counter() - t0

    # Прогрев: первый вызов onnxruntime всегда медленнее
    t0 = time.perf_counter()
    engine.synth("Hello.", spec.get("voice"))
    warmup_s = time.perf_counter() - t0

    sentences = split_sentences(BENCH_TEXT)
    per_sentence = []
    pieces = []
    for s in sentences:
        t0 = time.perf_counter()
        audio = engine.synth(s, spec.get("voice"))
        synth_s = time.perf_counter() - t0
        audio_s = len(audio) / engine.sample_rate
        per_sentence.append({"text": s, "synth_s": round(synth_s, 3), "audio_s": round(audio_s, 3)})
        a = np.asarray(audio)
        if a.dtype == np.int16:
            a = a.astype(np.float32) / 32767
        pieces.append(a)
        pieces.append(np.zeros(int(0.3 * engine.sample_rate), dtype=np.float32))

    wav_path = out_dir / f"bench_{name}.wav"
    write_wav(wav_path, np.concatenate(pieces), engine.sample_rate)

    synth_total = sum(p["synth_s"] for p in per_sentence)
    audio_total = sum(p["audio_s"] for p in per_sentence)

    # Живое чтение: успевает ли следующее предложение синтезироваться,
    # пока звучит текущее. Считаем максимальную паузу-заикание.
    clock = per_sentence[0]["synth_s"]       # момент начала звучания 1-го предложения
    play_end = clock + per_sentence[0]["audio_s"]
    max_gap = 0.0
    ready = clock
    for p in per_sentence[1:]:
        ready = ready + p["synth_s"]            # синтез идёт последовательно, без остановок
        start = max(ready, play_end)
        max_gap = max(max_gap, start - play_end)
        play_end = start + p["audio_s"]

    return {
        "engine": name,
        "title": spec["title"],
        "load_s": round(load_s, 2),
        "warmup_s": round(warmup_s, 2),
        "first_sentence_s": per_sentence[0]["synth_s"],
        "synth_total_s": round(synth_total, 2),
        "audio_total_s": round(audio_total, 2),
        "x_realtime": round(audio_total / synth_total, 2) if synth_total else None,
        "max_stall_s": round(max_gap, 2),
        "peak_ram_mb": round(peak_rss_mb()),
        "sentences": per_sentence,
        "wav": str(wav_path.relative_to(ROOT)),
    }


def worker_samples(kind: str, out_dir: Path) -> dict:
    made = []
    if kind == "kokoro":
        engine = KokoroEngine(MODELS / "kokoro" / "kokoro-fp32.onnx")
        for v in KOKORO_SAMPLE_VOICES:
            write_wav(out_dir / f"kokoro_{v}.wav", engine.synth(SAMPLE_TEXT, v), engine.sample_rate)
            made.append(f"kokoro_{v}.wav")
        # Медленный темп для младших классов
        write_wav(out_dir / "kokoro_af_heart_slow.wav", engine.synth(SAMPLE_TEXT, "af_heart", 0.8),
                  engine.sample_rate)
        made.append("kokoro_af_heart_slow.wav")
    else:
        for v in PIPER_SAMPLE_VOICES:
            path = MODELS / "piper" / f"{v}.onnx"
            if not path.exists():
                continue
            engine = PiperEngine(path)
            write_wav(out_dir / f"piper_{v}.wav", engine.synth(SAMPLE_TEXT), engine.sample_rate)
            made.append(f"piper_{v}.wav")
    return {"samples": made}


# ---------------------------------------------------------------- отчёт

def verdict(r: dict) -> str:
    x = r.get("x_realtime") or 0
    if x >= 2 and r["first_sentence_s"] <= 2 and r["max_stall_s"] < 0.3:
        return "годится для живого чтения"
    if x >= 1.1 and r["first_sentence_s"] <= 4:
        return "живое чтение на грани (заметная задержка на старте)"
    return "только заранее подготовленная озвучка"


def format_report(info: dict, results: list[dict]) -> str:
    lines = [
        f"Бенчмарк офлайн-TTS — {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"Компьютер: {info['computer']}",
        f"ОС: {info['os']}, {info['machine']}",
        f"CPU: {info['cpu']} ({info['logical_cpus']} логич. ядер)",
        f"RAM: {info['ram_gb']} ГБ, Python {info['python']}",
        "",
        "×РВ — во сколько раз синтез быстрее реального времени (больше — лучше).",
        "Старт — сколько ждать звука первого предложения после нажатия Play.",
        "Заикание — самая длинная пауза между предложениями при чтении «на лету».",
        "",
    ]
    for r in results:
        if "error" in r:
            lines += [f"■ {ENGINES[r['engine']]['title']}", f"   ОШИБКА: {r['error']}", ""]
            continue
        lines += [
            f"■ {r['title']}",
            f"   ×РВ: {r['x_realtime']}   старт: {r['first_sentence_s']:.2f} с   "
            f"заикание: {r['max_stall_s']:.2f} с",
            f"   загрузка модели: {r['load_s']:.1f} с, прогрев: {r['warmup_s']:.1f} с, "
            f"память: {r['peak_ram_mb']} МБ",
            f"   {r['audio_total_s']:.1f} с речи синтезировано за {r['synth_total_s']:.1f} с",
            f"   → {verdict(r)}",
            f"   файл: {r['wav']}",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def run_child(args: list[str], timeout: int) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"не уложился в {timeout // 60} мин"}
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("@@RESULT "):
            return json.loads(line[len("@@RESULT "):])
    tail = (p.stderr or p.stdout).strip().splitlines()[-5:]
    return {"error": f"процесс завершился с кодом {p.returncode}: " + " | ".join(tail)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default=",".join(ENGINES),
                    help="через запятую: " + ", ".join(ENGINES))
    ap.add_argument("--samples", action="store_true", help="озвучить образцы всех голосов")
    ap.add_argument("--selftest", action="store_true", help="быстрая проверка, что всё запускается")
    ap.add_argument("--timeout", type=int, default=900, help="лимит на один движок, секунд")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--worker-samples", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.worker or a.worker_samples:
        try:
            if a.worker:
                res = worker_bench(a.worker, Path(a.out))
            else:
                res = worker_samples(a.worker_samples, Path(a.out))
        except Exception as e:  # noqa: BLE001 — отдать ошибку родителю
            res = {"engine": a.worker, "error": f"{type(e).__name__}: {e}"}
        print("@@RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
        return 0

    info = system_info()
    out_dir = RESULTS / re.sub(r"[^\w.-]", "_", info["computer"] or "pc")
    out_dir.mkdir(parents=True, exist_ok=True)

    if a.selftest:
        ok = True
        for kind, model in (("kokoro", "kokoro/kokoro-int8.onnx"), ("piper", "piper/en_US-lessac-medium.onnx")):
            try:
                e = make_engine(kind, MODELS / model)
                audio = e.synth("This is a test.", "af_heart")
                print(f"  {kind}: OK, {len(audio) / e.sample_rate:.1f} с звука")
            except Exception as ex:  # noqa: BLE001
                ok = False
                print(f"  {kind}: ОШИБКА {type(ex).__name__}: {ex}")
        return 0 if ok else 1

    print(f"Компьютер: {info['computer']} | {info['cpu']} | RAM {info['ram_gb']} ГБ")

    if a.samples:
        print("Озвучиваю образцы голосов...")
        for kind in ("kokoro", "piper"):
            r = run_child(["--worker-samples", kind, "--out", str(out_dir / "samples")], a.timeout)
            print(f"  {kind}: {r.get('error') or ', '.join(r['samples'])}")
        print(f"\nОбразцы лежат в {out_dir / 'samples'}")
        return 0

    results = []
    names = [n.strip() for n in a.engines.split(",") if n.strip()]
    for i, name in enumerate(names, 1):
        if name not in ENGINES:
            print(f"Неизвестный движок {name}, пропускаю")
            continue
        print(f"[{i}/{len(names)}] {ENGINES[name]['title']}... ", end="", flush=True)
        t0 = time.perf_counter()
        r = run_child(["--worker", name, "--out", str(out_dir)], a.timeout)
        r.setdefault("engine", name)
        results.append(r)
        if "error" in r:
            print(f"ошибка: {r['error']}")
        else:
            print(f"×РВ {r['x_realtime']}, старт {r['first_sentence_s']:.2f} с "
                  f"({time.perf_counter() - t0:.0f} с)")

    report = format_report(info, results)
    (out_dir / "results.txt").write_text(report, encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps({"system": info, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + report)
    print(f"Отчёт и wav-файлы: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
