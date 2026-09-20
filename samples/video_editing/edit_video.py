#!/usr/bin/env python3
"""영상 편집 자동화 파이프라인.

무음 구간 자동 컷 편집 -> OpenAI Whisper API로 자막 생성 -> (선택) MZ 말투로 재작성
-> (선택) 예능풍 줌펀치+강조 스탬프 효과 -> 자막 굽기(burn-in) 순서로 동작한다.
무음 컷 편집은 CPU만으로 동작하며(GPU 불필요), 자막 생성/MZ 말투 변환/강조 포인트
추출은 OpenAI API를 사용한다.

사용 예:
    python edit_video.py inbox/my_video.mp4 -o outbox/my_video_edited.mp4

    # 자막 없이 무음 컷만
    python edit_video.py inbox/my_video.mp4 --no-captions

    # 자막을 MZ 말투로 재작성 + 예능풍 줌펀치/강조 스탬프
    python edit_video.py inbox/my_video.mp4 --mz-style --variety-fx

    # 자막 스타일 지정 (폰트/크기/색상/위치)
    python edit_video.py inbox/my_video.mp4 \
        --font "NanumGothic" --font-size 28 --font-color "&H00FFFFFF" --caption-position bottom
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile

from dotenv import load_dotenv

load_dotenv()
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class SilenceInterval:
    start: float
    end: float


@dataclass
class CaptionSegment:
    index: int
    start: float
    end: float
    text: str


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        raise


def ffprobe_duration(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(result.stdout.strip())


def ffprobe_dimensions(path: Path) -> tuple[int, int]:
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(path),
    ])
    width, height = result.stdout.strip().split("x")
    return int(width), int(height)


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


SRT_BLOCK_RE = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL,
)


def _srt_timestamp_to_seconds(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _seconds_to_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(srt_text: str) -> list[CaptionSegment]:
    segments = []
    for match in SRT_BLOCK_RE.finditer(srt_text.strip()):
        index, start, end, text = match.groups()
        segments.append(CaptionSegment(
            index=int(index),
            start=_srt_timestamp_to_seconds(start),
            end=_srt_timestamp_to_seconds(end),
            text=text.strip().replace("\n", " "),
        ))
    return segments


def format_srt(segments: list[CaptionSegment]) -> str:
    blocks = []
    for seg in segments:
        blocks.append(
            f"{seg.index}\n"
            f"{_seconds_to_srt_timestamp(seg.start)} --> {_seconds_to_srt_timestamp(seg.end)}\n"
            f"{seg.text}\n"
        )
    return "\n".join(blocks)


def _openai_chat_json(system_prompt: str, user_prompt: str) -> dict:
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return json.loads(response.choices[0].message.content)


def rewrite_mz_style(segments: list[CaptionSegment]) -> list[CaptionSegment]:
    """자막 텍스트를 한국 MZ세대 말투(신조어/캐주얼한 구어체)로 재작성한다."""
    lines = {str(seg.index): seg.text for seg in segments}
    system_prompt = (
        "너는 한국어 자막을 MZ세대 말투(캐주얼한 구어체, 신조어, 유행어 섞은 톤)로 "
        "재작성하는 편집자야. 의미는 최대한 유지하되 말투만 바꿔. "
        "각 줄은 원문과 길이가 비슷하게 유지해서 자막 타이밍이 안 깨지게 해줘. "
        "입력은 {\"1\": \"원문\", \"2\": \"원문\", ...} 형식의 JSON이고, "
        "출력도 같은 키(줄 번호)에 재작성된 텍스트를 담은 JSON으로만 답해줘."
    )
    result = _openai_chat_json(system_prompt, json.dumps(lines, ensure_ascii=False))

    rewritten = []
    for seg in segments:
        new_text = result.get(str(seg.index), seg.text)
        rewritten.append(CaptionSegment(index=seg.index, start=seg.start, end=seg.end, text=new_text))
    return rewritten


@dataclass
class EmphasisMoment:
    start: float
    end: float
    stamp: str


def detect_emphasis_moments(segments: list[CaptionSegment], max_moments: int = 6) -> list[EmphasisMoment]:
    """예능 자막처럼, 놀라움/강조 포인트에 넣을 짧은 스탬프 텍스트와 타이밍을 뽑는다."""
    lines = [{"index": seg.index, "start": seg.start, "end": seg.end, "text": seg.text} for seg in segments]
    system_prompt = (
        "너는 한국 예능 자막 PD야. 아래 자막 목록(각 줄에 index/start/end 초 단위 타임스탬프/text)을 보고, "
        f"가장 놀랍거나 강조할 만한 순간을 최대 {max_moments}개 골라줘. "
        "각 순간마다 화면에 잠깐 띄울 짧은 강조 단어(예: '헐', '대박', '충격', '레전드', '?!', '!!')를 하나 골라줘. "
        "출력은 반드시 JSON으로: "
        "{\"moments\": [{\"start\": 숫자, \"end\": 숫자, \"stamp\": \"단어\"}, ...]} 형식으로만 답해줘. "
        "end는 start보다 0.5~1.0초 정도 뒤로 잡아줘."
    )
    result = _openai_chat_json(system_prompt, json.dumps(lines, ensure_ascii=False))

    moments = []
    for m in result.get("moments", [])[:max_moments]:
        try:
            moments.append(EmphasisMoment(start=float(m["start"]), end=float(m["end"]), stamp=str(m["stamp"])))
        except (KeyError, ValueError, TypeError):
            continue
    return moments


CAPTION_ALIGNMENT = {"bottom": 2, "top": 8, "middle": 5}


def _escape_ffmpeg_path(path: Path) -> str:
    # ffmpeg의 필터그래프 문법에서는 콜론(:)이 옵션 구분자, 백슬래시(\)가 이스케이프 문자로
    # 쓰이므로, Windows 경로("C:\..." 또는 "outbox\..." 형태)를 그대로 넣으면 깨진다.
    # 슬래시로 통일하고 드라이브 문자 뒤 콜론만 이스케이프해서 넘긴다.
    return str(path).replace("\\", "/").replace(":", r"\:")


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def default_stamp_font_file() -> str | None:
    if platform.system() == "Windows":
        return r"C:\Windows\Fonts\malgunbd.ttf"
    return None


def build_zoom_punch_filter(moments: list[EmphasisMoment], width: int, height: int, zoom_amount: float) -> str:
    """강조 순간마다 화면을 살짝 확대하는 '줌펀치' 효과(예능 편집 스타일)를 만든다."""
    terms = "+".join(f"between(t,{m.start:.3f},{m.end:.3f})" for m in moments)
    zoom_expr = f"(1+{zoom_amount}*({terms}))"
    crop = (
        f"crop=w='in_w/{zoom_expr}':h='in_h/{zoom_expr}':"
        f"x='(in_w-out_w)/2':y='(in_h-out_h)/2'"
    )
    return f"{crop},scale={width}:{height}"


def build_stamp_filters(moments: list[EmphasisMoment], font_size: int, font_file: str | None) -> list[str]:
    """강조 순간마다 화면 위쪽에 짧게 뜨는 강조 스탬프 텍스트(drawtext)를 만든다."""
    filters = []
    font_option = f"fontfile='{_escape_ffmpeg_path(Path(font_file))}'" if font_file else "font='NanumGothic:bold'"
    for m in moments:
        text = _escape_drawtext(m.stamp)
        filters.append(
            f"drawtext={font_option}:text='{text}':fontsize={font_size}:fontcolor=yellow:"
            "borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.12:"
            f"enable='between(t,{m.start:.3f},{m.end:.3f})'"
        )
    return filters


def render_final(
    src: Path, srt_path: Path, dst: Path, font: str, font_size: int,
    font_color: str, outline_color: str, position: str,
    zoom_moments: list[EmphasisMoment] | None = None,
    stamp_moments: list[EmphasisMoment] | None = None,
    zoom_amount: float = 0.18,
    stamp_font_size: int = 64,
    stamp_font_file: str | None = None,
) -> None:
    alignment = CAPTION_ALIGNMENT.get(position, 2)
    style = (
        f"FontName={font},FontSize={font_size},PrimaryColour={font_color},"
        f"OutlineColour={outline_color},BorderStyle=1,Outline=2,Alignment={alignment}"
    )
    srt_escaped = _escape_ffmpeg_path(srt_path)

    stages = []
    if zoom_moments:
        width, height = ffprobe_dimensions(src)
        stages.append(build_zoom_punch_filter(zoom_moments, width, height, zoom_amount))
    stages.append(f"subtitles={srt_escaped}:force_style='{style}'")
    if stamp_moments:
        stages.extend(build_stamp_filters(stamp_moments, stamp_font_size, stamp_font_file))

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", ",".join(stages),
        "-c:a", "copy",
        str(dst),
    ]
    run(cmd)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="편집할 원본 영상 경로 (예: inbox/video.mp4)")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="결과 영상 경로 (기본: <이 스크립트 폴더>/outbox/<입력파일명>)",
    )

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
    caption_group.add_argument(
        "--mz-style", action="store_true",
        help="Whisper로 뽑은 자막을 OpenAI Chat API로 한국 MZ세대 말투로 재작성 (추가 API 비용 발생)",
    )

    fx_group = parser.add_argument_group("예능풍 효과 (--variety-fx)")
    fx_group.add_argument(
        "--variety-fx", action="store_true",
        help="자막 내용을 분석해 강조 포인트에 줌펀치 + 강조 스탬프 텍스트를 추가 (자막 필요, 추가 API 비용 발생)",
    )
    fx_group.add_argument("--zoom-amount", type=float, default=0.18, help="줌펀치 확대 비율 (기본 0.18 = 18%%)")
    fx_group.add_argument("--stamp-font-size", type=int, default=64, help="강조 스탬프 글자 크기 (기본 64)")
    fx_group.add_argument(
        "--stamp-font-file", default=None,
        help="강조 스탬프에 쓸 폰트 파일 경로. 미지정 시 Windows는 맑은 고딕, 그 외는 폰트 이름으로 자동 지정",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.input.exists():
        print(f"입력 파일을 찾을 수 없습니다: {args.input}", file=sys.stderr)
        return 1

    if args.variety_fx and args.no_captions:
        print("--variety-fx는 자막(캡션)이 있어야 동작합니다. --no-captions와 함께 쓸 수 없습니다.", file=sys.stderr)
        return 1

    output = args.output or SCRIPT_DIR / "outbox" / args.input.name
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        working = args.input

        if not args.no_silence_cut:
            print(f"[1/4] 무음 구간 탐지 중... (noise={args.noise_db}dB, min={args.min_silence}s)")
            duration = ffprobe_duration(working)
            silences = detect_silence(working, args.noise_db, args.min_silence)
            keep_ranges = build_keep_ranges(silences, duration, args.padding, args.min_keep)
            print(f"      무음 {len(silences)}개 구간 감지 -> 남길 구간 {len(keep_ranges)}개")

            cut_path = tmp_dir / "cut.mp4"
            cut_silence(working, cut_path, keep_ranges)
            working = cut_path
        else:
            print("[1/4] 무음 컷 편집 건너뜀 (--no-silence-cut)")

        if not args.no_captions:
            print("[2/4] 오디오 추출 및 Whisper API 자막 생성 중...")
            audio_path = tmp_dir / "audio.mp3"
            extract_audio(working, audio_path)
            srt_text = transcribe_to_srt(audio_path, args.language)
            segments = parse_srt(srt_text)

            if args.mz_style:
                print("      자막을 MZ 말투로 재작성 중...")
                segments = rewrite_mz_style(segments)
                srt_text = format_srt(segments)

            srt_path = args.srt_out or output.with_suffix(".srt")
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(srt_text, encoding="utf-8")
            print(f"      자막 저장: {srt_path}")

            zoom_moments = stamp_moments = None
            if args.variety_fx:
                print("[3/4] 강조 포인트 분석 중 (줌펀치/스탬프)...")
                zoom_moments = stamp_moments = detect_emphasis_moments(segments)
                print(f"      강조 포인트 {len(zoom_moments)}개 감지")
            else:
                print("[3/4] 예능풍 효과 건너뜀 (--variety-fx 아님)")

            print("[4/4] 자막 굽는 중...")
            render_final(
                working, srt_path, output,
                args.font, args.font_size, args.font_color, args.outline_color, args.caption_position,
                zoom_moments=zoom_moments, stamp_moments=stamp_moments,
                zoom_amount=args.zoom_amount, stamp_font_size=args.stamp_font_size,
                stamp_font_file=args.stamp_font_file or default_stamp_font_file(),
            )
        else:
            print("[2/4] 자막 생성 건너뜀 (--no-captions)")
            print("[3/4] 예능풍 효과 건너뜀 (자막 없음)")
            print("[4/4] 최종 파일로 복사")
            run(["ffmpeg", "-y", "-i", str(working), "-c", "copy", str(output)])

    print(f"완료: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
