"""OpenAI 이미지 생성 API로 여러 아이콘/이미지를 병렬 생성하는 스크립트.

순차 호출 시 이미지 하나당 대기 시간이 그대로 누적되는 문제를,
asyncio + Semaphore로 여러 요청을 동시에 보내 단축한다.

사용법:
    python samples/generate_icons.py \
        --prompts samples/icons_prompts.json \
        --output-dir samples/output_icons \
        --concurrency 5
"""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEFAULT_MODEL = "gpt-image-1"
DEFAULT_SIZE = "1024x1024"


async def generate_one(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    prompt: str,
    index: int,
    output_dir: Path,
    model: str,
    size: str,
) -> Path:
    async with semaphore:
        result = await client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            n=1,
        )
        image_bytes = base64.b64decode(result.data[0].b64_json)
        output_path = output_dir / f"icon_{index:03d}.png"
        output_path.write_bytes(image_bytes)
        print(f"[done] {output_path.name}: {prompt[:50]}")
        return output_path


async def generate_all(
    prompts: list[str],
    output_dir: Path,
    model: str,
    size: str,
    concurrency: int,
) -> list[Path]:
    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(concurrency)
    output_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        generate_one(client, semaphore, prompt, i, output_dir, model, size)
        for i, prompt in enumerate(prompts)
    ]
    return await asyncio.gather(*tasks)


def load_prompts(path: str) -> list[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["prompts"] if isinstance(data, dict) else data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenAI 이미지 생성 API로 여러 아이콘을 병렬 생성"
    )
    parser.add_argument(
        "--prompts",
        default="samples/icons_prompts.json",
        help="프롬프트 목록이 담긴 JSON 파일 경로",
    )
    parser.add_argument(
        "--output-dir",
        default="samples/output_icons",
        help="생성된 이미지를 저장할 디렉터리",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="동시에 보낼 최대 요청 수 (API 레이트 리밋에 맞춰 조절)",
    )
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY가 설정되어 있지 않습니다. .env 파일을 확인하세요.")

    prompts = load_prompts(args.prompts)
    output_dir = Path(args.output_dir)

    results = asyncio.run(
        generate_all(prompts, output_dir, args.model, args.size, args.concurrency)
    )
    print(f"\n총 {len(results)}개 이미지 생성 완료 -> {output_dir}")


if __name__ == "__main__":
    main()
