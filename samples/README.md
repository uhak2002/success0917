# samples

예제 코드와 샘플 데이터를 이 폴더에 보관합니다.

## generate_icons.py

OpenAI 이미지 생성 API(`gpt-image-1`)를 사용해 여러 아이콘/이미지를 **병렬**로 생성합니다.
순차 호출 대비 전체 소요 시간을 크게 줄일 수 있습니다.

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY 값 채우기

python samples/generate_icons.py \
  --prompts samples/icons_prompts.json \
  --output-dir samples/output_icons \
  --concurrency 5
```

- `--prompts`: 프롬프트 목록이 담긴 JSON 파일 (`{"prompts": [...]}` 또는 문자열 배열)
- `--concurrency`: 동시에 보낼 최대 요청 수. API 레이트 리밋에 맞춰 조절하세요.
- 결과 이미지는 `samples/output_icons/`에 저장되며(git에는 커밋되지 않음), `icons_prompts.json`에 프롬프트를 추가/수정해서 재사용할 수 있습니다.

## video_editing/

영상의 무음 구간을 자동으로 잘라내고, Whisper API로 자막을 생성해 입혀주는 영상 편집 자동화 파이프라인입니다.
자세한 내용은 [`video_editing/README.md`](video_editing/README.md) 참고.
