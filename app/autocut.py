import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Tuple, Dict, Any


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


MODES = {
    "natural": {
        "min_silence": 0.65,
        "keep_silence": 0.20,
        "threshold_db": -38.0,
        "padding": 0.08,
    },
    "standard": {
        "min_silence": 0.42,
        "keep_silence": 0.12,
        "threshold_db": -35.0,
        "padding": 0.06,
    },
    "tight": {
        "min_silence": 0.28,
        "keep_silence": 0.08,
        "threshold_db": -32.0,
        "padding": 0.04,
    },
}


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"未找到 {name}。请先安装 ffmpeg，并确保 {name} 可在命令行访问。")


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


def probe_duration(input_path: Path) -> float:
    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe 获取视频时长失败")
    return float(result.stdout.strip())


def detect_silences(input_path: Path, threshold_db: float, min_silence: float) -> List[Segment]:
    result = run([
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(input_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
        "-f", "null",
        "-",
    ])
    text = result.stderr
    if result.returncode != 0 and "silencedetect" not in text:
        raise RuntimeError(text.strip() or "ffmpeg 静音检测失败")

    starts = []
    silences = []
    for line in text.splitlines():
        m_start = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_start:
            starts.append(float(m_start.group(1)))
            continue
        m_end = re.search(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", line)
        if m_end:
            end = float(m_end.group(1))
            duration = float(m_end.group(2))
            start = starts.pop(0) if starts else end - duration
            silences.append(Segment(max(0.0, start), max(0.0, end)))
    return silences


def merge_segments(segments: List[Segment], gap: float = 0.03) -> List[Segment]:
    if not segments:
        return []
    segments = sorted(segments, key=lambda s: s.start)
    merged = [segments[0]]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.start <= last.end + gap:
            last.end = max(last.end, seg.end)
        else:
            merged.append(seg)
    return [s for s in merged if s.duration > 0.02]


def build_keep_segments(duration: float, silences: List[Segment], keep_silence: float, padding: float) -> Tuple[List[Segment], List[Segment]]:
    keep = []
    removed = []
    cursor = 0.0

    for silence in silences:
        if silence.end <= cursor:
            continue

        voice_end = max(cursor, silence.start + padding)
        if voice_end > cursor:
            keep.append(Segment(cursor, min(voice_end, duration)))

        natural_keep = min(keep_silence, silence.duration)
        keep_start = max(0.0, silence.start - padding)
        keep_end = min(duration, keep_start + natural_keep)
        if keep_end > keep_start:
            keep.append(Segment(keep_start, keep_end))

        remove_start = keep_end
        remove_end = max(remove_start, silence.end - padding)
        if remove_end > remove_start:
            removed.append(Segment(remove_start, remove_end))

        cursor = max(cursor, silence.end - padding)

    if cursor < duration:
        keep.append(Segment(cursor, duration))

    return merge_segments(keep), merge_segments(removed)


def make_filter_complex(segments: List[Segment]) -> str:
    parts = []
    concat_inputs = []
    for i, seg in enumerate(segments):
        parts.append(
            f"[0:v]trim=start={seg.start:.3f}:end={seg.end:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={seg.start:.3f}:end={seg.end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")
    return ";".join(parts) + ";" + "".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"


def export_video(input_path: Path, output_path: Path, segments: List[Segment]) -> None:
    if not segments:
        raise RuntimeError("没有可保留片段，无法导出。请降低静音阈值或使用 natural 模式。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = make_filter_complex(segments)
    result = run([
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg 导出失败")


def create_report(input_path: Path, output_path: Path, duration: float, keep: List[Segment], removed: List[Segment], params: Dict[str, Any]) -> Dict[str, Any]:
    kept_duration = sum(s.duration for s in keep)
    removed_duration = sum(s.duration for s in removed)
    return {
        "input": str(input_path),
        "output": str(output_path),
        "original_duration": round(duration, 3),
        "estimated_output_duration": round(kept_duration, 3),
        "removed_duration": round(removed_duration, 3),
        "compression_ratio": round(kept_duration / duration, 4) if duration else 0,
        "params": params,
        "keep_segments": [asdict(s) for s in keep],
        "removed_segments": [asdict(s) for s in removed],
    }


def autocut(input_file: str, output_file: str = None, mode: str = "standard", min_silence: float = None, keep_silence: float = None, threshold_db: float = None, padding: float = None) -> Dict[str, Any]:
    require_binary("ffmpeg")
    require_binary("ffprobe")

    input_path = Path(input_file).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")

    params = dict(MODES.get(mode, MODES["standard"]))
    if min_silence is not None:
        params["min_silence"] = float(min_silence)
    if keep_silence is not None:
        params["keep_silence"] = float(keep_silence)
    if threshold_db is not None:
        params["threshold_db"] = float(threshold_db)
    if padding is not None:
        params["padding"] = float(padding)
    params["mode"] = mode

    if output_file:
        output_path = Path(output_file).expanduser().resolve()
    else:
        output_path = input_path.with_name(input_path.stem + f"_autocut_{mode}.mp4")

    duration = probe_duration(input_path)
    silences = detect_silences(input_path, params["threshold_db"], params["min_silence"])
    keep, removed = build_keep_segments(duration, silences, params["keep_silence"], params["padding"])
    export_video(input_path, output_path, keep)

    report = create_report(input_path, output_path, duration, keep, removed, params)
    report_path = output_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="口播视频自动去停顿粗剪工具")
    parser.add_argument("input", help="输入视频路径")
    parser.add_argument("--output", help="输出视频路径")
    parser.add_argument("--mode", choices=list(MODES.keys()), default="standard")
    parser.add_argument("--min-silence", type=float, default=None)
    parser.add_argument("--keep-silence", type=float, default=None)
    parser.add_argument("--threshold-db", type=float, default=None)
    parser.add_argument("--padding", type=float, default=None)
    args = parser.parse_args()

    try:
        report = autocut(
            args.input,
            args.output,
            mode=args.mode,
            min_silence=args.min_silence,
            keep_silence=args.keep_silence,
            threshold_db=args.threshold_db,
            padding=args.padding,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
