import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List


FILLER_WORDS = [
    "嗯",
    "呃",
    "啊",
    "额",
    "这个",
    "那个",
    "然后",
    "就是",
    "其实",
    "就是说",
    "对吧",
    "你知道吧",
    "是不是",
    "怎么说呢",
]


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class FillerHit:
    start: float
    end: float
    text: str
    type: str = "filler"
    source_text: str = ""


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"未找到 {name}。请先安装 ffmpeg，并确保 {name} 可在命令行访问。")


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


def extract_audio(video_path: Path, audio_path: Path) -> None:
    result = run([
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(audio_path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "音频提取失败")


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis -= 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: List[TranscriptSegment], srt_path: Path) -> None:
    lines = []
    for index, seg in enumerate(segments, start=1):
        text = seg.text.strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{format_srt_time(seg.start)} --> {format_srt_time(seg.end)}")
        lines.append(text)
        lines.append("")
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def detect_fillers(segments: List[TranscriptSegment]) -> List[FillerHit]:
    hits: List[FillerHit] = []
    escaped = sorted((re.escape(word) for word in FILLER_WORDS), key=len, reverse=True)
    pattern = re.compile("|".join(escaped))

    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        if not compact:
            continue
        duration = max(0.01, seg.end - seg.start)
        char_duration = duration / max(1, len(compact))
        for match in pattern.finditer(compact):
            word = match.group(0)
            start = seg.start + match.start() * char_duration
            end = seg.start + max(match.end(), match.start() + 1) * char_duration
            hits.append(FillerHit(
                start=round(start, 3),
                end=round(end, 3),
                text=word,
                source_text=text,
            ))
    return hits


def transcribe_video(video_file: str, output_dir: str, model_size: str = "base") -> Dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "当前未安装 faster-whisper，无法分析口播瑕疵。请先运行：pip install faster-whisper"
        ) from exc

    require_binary("ffmpeg")
    video_path = Path(video_file).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"视频文件不存在：{video_path}")

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(tmp) / "audio.wav"
        extract_audio(video_path, audio_path)

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        raw_segments, info = model.transcribe(str(audio_path), language="zh", vad_filter=True)

        segments = [
            TranscriptSegment(
                start=round(float(seg.start), 3),
                end=round(float(seg.end), 3),
                text=seg.text.strip(),
            )
            for seg in raw_segments
            if seg.text and seg.text.strip()
        ]

    fillers = detect_fillers(segments)
    base_name = video_path.stem
    srt_path = output_path / f"{base_name}.srt"
    json_path = output_path / f"{base_name}_speech.json"
    write_srt(segments, srt_path)

    payload = {
        "video": str(video_path),
        "model": model_size,
        "language": "zh",
        "duration": getattr(info, "duration", None),
        "segments": [asdict(seg) for seg in segments],
        "fillers": [asdict(hit) for hit in fillers],
        "srt": str(srt_path),
        "json": str(json_path),
        "filler_words": FILLER_WORDS,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
