# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code(및 기타 AI 코딩 에이전트)를 위한 가이드입니다.

## 프로젝트 개요
success0917 프로젝트. 세부 내용은 [README.md](README.md)를 참고하세요.

## 작업 규칙
- `.env` 파일은 절대 커밋하지 않습니다. 새로운 환경 변수가 필요하면 `.env.example`에도 함께 추가합니다.
- 새 의존성을 추가하면 `requirements.txt`에 반영합니다.
- 문서는 `docs/` 폴더에, 예제/샘플 코드는 `samples/` 폴더에 둡니다.
- 커밋 메시지는 변경 이유를 간결하게 설명합니다.
- **작업을 시작할 때 항상 `docs/ai-context/`를 먼저 읽고 기존 기록을 확인합니다.** (`MEMORY.md`가 목차입니다.)
- **사용자가 지적하거나 수정을 요구한 내용은 `docs/ai-context/feedback-{주제}.md` 파일로 기록하고, `docs/ai-context/MEMORY.md` 목차에 한 줄로 등록합니다.**

## 폴더 구조
- `docs/`: 설계 문서, 사용법, 참고 자료
  - `docs/ai-context/`: 작업 중 누적되는 AI 작업 기록 (`MEMORY.md`가 목차, `feedback-{주제}.md`가 개별 항목)
- `samples/`: 예제 스크립트, 샘플 데이터
- `agent.md`: 에이전트(자동화 워크플로) 관련 문서
