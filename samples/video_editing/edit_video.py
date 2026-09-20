#!/usr/bin/env python3
"""영상 편집 자동화 파이프라인.

무음 구간 자동 컷 편집 -> (선택) OpenAI Whisper API로 자막 생성 -> 자막 굽기(burn-in)
순서로 동작한다. 전부 CPU만으로 동작하며(ffmpeg + Whisper API), GPU는 필요 없다.

사용 예:
    python edit_video.py inbox/my_video.mp4 -o outbox/my_video_edited.mp4

    # 자막 없이 무음 컷만
    python edit_video.py inbox/my_video.mp4 --no-captions

    # 자막 스타일 지정 (폰트/크기/색상/위치)
    python edit_video.py inbox/my_video.mp4 \
        --font "NanumGothic" --font-size 28 --font-color "&H00FFFFFF" --caption-position bottom
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile

from dotenv import load_dotenv

load_dotenv()
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SilenceInterval:
    start: float
    end: float


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)


def ffprobe_duration(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(result.stdout.strip())


def detect_silence(path: Path, noise_db: float, min_duration: float) -> list[SilenceInterval]:
    """ffmpeg silencedetect 필터로 무음 구간을 찾는다."""
    cmd = [
        "ffmpeg", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    log = proc.stderr

    starts = [float(m) for m in re.findall(r"silence_start:\s*([-\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([-\d.]+)", log)]

    intervals = []
    for start, end in zip(starts, ends):
        intervals.append(SilenceInterval(start, end))
    return intervals


def build_keep_ranges(
    silences: list[SilenceInterval], duration: float, padding: float, min_keep: float,
) -> list[tuple[float, float]]:
    """무음 구간의 여집합(=남길 구간)을 계산한다. padding만큼 여유를 두어 말이 잘리지 않게 한다."""
    cuts = []
    for s in silences:
        start = max(0.0, s.start + padding)
        end = min(duration, s.end - padding)
        if end > start:
            cuts.append((start, end))

    cuts.sort()
    merged: list[tuple[float, float]] = []
    for start, end in cuts:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    keep = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            keep.append((cursor, start))
        cursor = end
    if cursor < duration:
        keep.append((cursor, duration))

    return [seg for seg in keep if seg[1] - seg[0] >= min_keep]


def cut_silence(
    src: Path, dst: Path, keep_ranges: list[tuple[float, float]],
) -> None:
    """select/aselect 필터로 무음 구간을 잘라내고 하나의 파일로 다시 이어붙인다."""
    if not keep_ranges:
        raise ValueError("남길 구간이 없습니다. 무음 임계값(--noise-db)을 조정해 보세요.")

    conditions = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep_ranges)
    video_filter = f"select='{conditions}',setpts=N/FRAME_RATE/TB"
    audio_filter = f"aselect='{conditions}',asetpts=N/SR/TB"

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", video_filter,
        "-af", audio_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    run(cmd)


def extract_audio(src: Path, dst: Path) -> None:
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-acodec", "libmp3lame", "-q:a", "4", str(dst)])


def transcribe_to_srt(audio_path: Path, language: str | None) -> str:
    """OpenAI Whisper API(audio.transcriptions)로 SRT 자막을 생성한다."""
    from openai import OpenAI

    client = OpenAI()
    with open(audio_path, "rb") as f:
        kwargs = {"model": "whisper-1", "file": f, "response_format": "srt"}
        if language:
            kwargs["language"] = language
        result = client.audio.transcriptions.create(**kwargs)
    return result if isinstance(result, str) else result.text


CAPTION_ALIGNMENT = {"bottom": 2, "top": 8, "middle": 5}


def burn_captions(
    src: Path, srt_path: Path, dst: Path, font: str, font_size: int,
    font_color: str, outline_color: str, position: str,
) -> None:
    alignment = CAPTION_ALIGNMENT.get(position, 2)
    style = (
        f"FontName={font},FontSize={font_size},PrimaryColour={font_color},"
        f"OutlineColour={outline_color},BorderStyle=1,Outline=2,Alignment={alignment}"
    )
    srt_escaped = str(srt_path).replace(":", r"\:")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"subtitles={srt_escaped}:force_style='{style}'",
        "-c:a", "copy",
        str(dst),
    ]
    run(cmd)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="편집할 원본 영상 경로 (예: inbox/video.mp4)")
    parser.add_argument("-o", "--output", type=Path, default=None, help="결과 영상 경로 (기본: outbox/<입력파일명>)")

    silence_group = parser.add_argument_group("무음 컷 편집")
    silence_group.add_argument("--noise-db", type=float, default=-30.0, help="무음으로 판단할 dB 임계값 (기본 -30)")
    silence_group.add_argument("--min-silence", type=float, default=0.6, help="무음으로 인정할 최소 길이(초, 기본 0.6)")
    silence_group.add_argument("--padding", type=float, default=0.15, help="컷 경계 여유(초, 기본 0.15) — 말 잘림 방지")
    silence_group.add_argument("--min-keep", type=float, default=0.2, help="이보다 짧은 구간은 버림(초, 기본 0.2)")
    silence_group.add_argument("--no-silence-cut", action="store_true", help="무음 컷 편집을 건너뜀")

    caption_group = parser.add_argument_group("자막")
    caption_group.add_argument("--no-captions", action="store_true", help="자막 생성을 건너뜀 (OpenAI API 불필요)")
    caption_group.add_argument("--language", default=None, help="자막 언어 코드 (예: ko, en). 미지정 시 자동 감지")
    caption_group.add_argument("--font", default="NanumGothic", help="자막 폰트 이름 (기본 NanumGothic)")
    caption_group.add_argument("--font-size", type=int, default=28, help="자막 크기 (기본 28)")
    caption_group.add_argument("--font-color", default="&H00FFFFFF", help="자막 글자색 (ASS BGR 형식, 기본 흰색)")
    caption_group.add_argument("--outline-color", default="&H00000000", help="자막 외곽선 색 (기본 검정)")
    caption_group.add_argument("--caption-position", choices=["bottom", "middle", "top"], default="bottom")
    caption_group.add_argument("--srt-out", type=Path, default=None, help="생성된 SRT를 저장할 경로 (기본: 출력 영상과 같은 이름.srt)")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.input.exists():
        print(f"입력 파일을 찾을 수 없습니다: {args.input}", file=sys.stderr)
        return 1

    output = args.output or Path("outbox") / args.input.name
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        working = args.input

        if not args.no_silence_cut:
            print(f"[1/3] 무음 구간 탐지 중... (noise={args.noise_db}dB, min={args.min_silence}s)")
            duration = ffprobe_duration(working)
            silences = detect_silence(working, args.noise_db, args.min_silence)
            keep_ranges = build_keep_ranges(silences, duration, args.padding, args.min_keep)
            print(f"      무음 {len(silences)}개 구간 감지 -> 남길 구간 {len(keep_ranges)}개")

            cut_path = tmp_dir / "cut.mp4"
            cut_silence(working, cut_path, keep_ranges)
            working = cut_path
        else:
            print("[1/3] 무음 컷 편집 건너뜀 (--no-silence-cut)")

        if not args.no_captions:
            print("[2/3] 오디오 추출 및 Whisper API 자막 생성 중...")
            audio_path = tmp_dir / "audio.mp3"
            extract_audio(working, audio_path)
            srt_text = transcribe_to_srt(audio_path, args.language)

            srt_path = args.srt_out or output.with_suffix(".srt")
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(srt_text, encoding="utf-8")
            print(f"      자막 저장: {srt_path}")

            print("[3/3] 자막 굽는 중...")
            burn_captions(
                working, srt_path, output,
                args.font, args.font_size, args.font_color, args.outline_color, args.caption_position,
            )
        else:
            print("[2/3] 자막 생성 건너뜀 (--no-captions)")
            print("[3/3] 최종 파일로 복사")
            run(["ffmpeg", "-y", "-i", str(working), "-c", "copy", str(output)])

    print(f"완료: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
