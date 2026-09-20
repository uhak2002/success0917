# video_editing — 영상 편집 자동화 워크플로우

강의 "집 밖에서도 가능한 영상 편집 자동으로 해줘" 과제로 만든 영상 편집 자동화 파이프라인입니다.
(배경 설명은 [`docs/video-editing-automation-lecture.md`](../../docs/video-editing-automation-lecture.md) 참고)

## 무엇을 자동화하나

1. **무음 구간 자동 컷 편집** — ffmpeg `silencedetect`로 무음 구간을 찾고, `select`/`aselect` 필터로 잘라낸 뒤 다시 하나로 이어붙입니다. GPU 없이 CPU만으로 동작합니다.
2. **자동 자막 생성** — 컷 편집이 끝난 오디오를 OpenAI Whisper API(`whisper-1`)로 보내 SRT 자막을 생성합니다. (API 키 필요)
3. **자막 굽기(burn-in)** — ffmpeg `subtitles` 필터(libass)로 폰트/크기/색상/위치를 지정해 자막을 영상에 입힙니다.

무거운 인코딩·업스케일링·얼굴 모자이크 같은 GPU 작업은 포함하지 않았습니다 (강의에서 "GPU가 좋은 로컬 PC에서 처리"하라고 안내한 부분 — 필요해지면 이 스크립트 뒤에 별도 단계로 추가하면 됩니다).

## 사용 방법

```bash
pip install -r requirements.txt   # openai, python-dotenv
cp .env.example .env              # OPENAI_API_KEY 채우기 (자막 생성 시 필요)

# 시스템에 ffmpeg 설치 필요 (로컬 PC: brew install ffmpeg / apt install ffmpeg)

# 영상을 inbox/에 넣고 실행
python samples/video_editing/edit_video.py samples/video_editing/inbox/my_video.mp4
# -> samples/video_editing/outbox/my_video.mp4 + my_video.srt 생성
```

### 주요 옵션

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--noise-db` | 무음으로 판단할 dB 임계값 | -30 |
| `--min-silence` | 무음으로 인정할 최소 길이(초) | 0.6 |
| `--padding` | 컷 경계 여유(초, 말 잘림 방지) | 0.15 |
| `--min-keep` | 이보다 짧은 구간은 버림(초) | 0.2 |
| `--no-silence-cut` | 무음 컷 편집 생략 | - |
| `--no-captions` | 자막 생성/굽기 생략 (API 키 불필요) | - |
| `--language` | 자막 언어 코드 (`ko`, `en` 등) | 자동 감지 |
| `--font`, `--font-size` | 자막 폰트/크기 | NanumGothic, 28 |
| `--font-color`, `--outline-color` | 자막 색상 (ASS `&HBBGGRR` 형식) | 흰색 / 검정 |
| `--caption-position` | `bottom` / `middle` / `top` | bottom |
| `-o, --output` | 출력 경로 | `outbox/<입력파일명>` |

예시:
```bash
# 자막 없이 무음 컷만
python samples/video_editing/edit_video.py inbox/video.mp4 --no-captions

# 자막 스타일 커스터마이즈
python samples/video_editing/edit_video.py inbox/video.mp4 \
  --font "NanumGothicBold" --font-size 32 --caption-position bottom --language ko
```

## inbox / outbox 폴더 (구글 드라이브 연동 개념)

강의에서 설명한 "구글 드라이브 inbox 업로드 → 클라우드에서 편집 → outbox 업로드" 흐름을 로컬 폴더 관례로 구현했습니다.

- `inbox/`: 편집할 원본 영상을 넣는 곳
- `outbox/`: 편집이 끝난 결과물이 나오는 곳
- 두 폴더 모두 실제 영상 파일은 git에 커밋하지 않습니다(`.gitignore` 처리, 용량 문제).

실제 구글 드라이브와 자동 동기화하려면 다음 중 하나로 확장하면 됩니다.
- **가장 간단한 방법**: 구글 드라이브 데스크톱 앱으로 `inbox`/`outbox` 폴더를 동기화 폴더로 지정.
- **API 연동**: Google Drive API(OAuth) + `google-api-python-client`로 inbox 폴더를 감시(polling)하다가 새 파일이 생기면 이 스크립트를 실행하고 결과를 outbox 폴더에 업로드하는 별도 스크립트 추가.

## 로컬 PC ↔ 클라우드(Claude Code) 연계

- **로컬 PC**: GPU/CPU가 좋다면 여기서 무거운 인코딩·컷 편집을 실행 (시간·비용 절약).
- **클라우드(Claude Code)**: 이 저장소를 깃허브에 올려두면, 컴퓨터를 꺼도 폰이나 다른 기기의 클라우드 세션에서 같은 코드로 이어서 작업 지시 가능. 클라우드 환경은 GPU가 없는 경우가 많으므로 가볍거나 짧은 영상 위주로 돌리는 것을 권장합니다.
- 클라우드 세션에서 구글 드라이브(`drive.google.com`)에 직접 접근하려면 해당 클라우드 환경의 네트워크 액세스 설정에 도메인을 허용해야 합니다.

## 테스트 방법 (실제 영상 없이)

ffmpeg로 소리 구간이 있는 합성 테스트 영상을 만들어 무음 컷 로직을 검증할 수 있습니다.

```bash
ffmpeg -f lavfi -i "testsrc=size=640x360:rate=25:duration=2" -f lavfi -i "sine=frequency=440:duration=2" \
  -c:v libx264 -c:a aac -ac 1 -shortest seg_sound.mp4
ffmpeg -f lavfi -i "testsrc=size=640x360:rate=25:duration=2" -f lavfi -i "anullsrc=r=44100:cl=mono:duration=2" \
  -c:v libx264 -c:a aac -ac 1 -shortest seg_silence.mp4
# 두 세그먼트를 concat demuxer로 이어붙인 뒤 edit_video.py로 테스트
```
