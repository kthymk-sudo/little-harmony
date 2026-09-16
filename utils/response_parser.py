# utils/response_parser.py
# ============================================================
# AI 응답 텍스트를 안전하게 파싱하는 전담 모듈.
# 하모니의 parse_almind_response()는 "$$" 구분자 방식이었으나,
# 이 프로젝트는 타겟 조건을 실제 필터링에 써야 하므로 JSON 코드블록
# 추출 방식(parse_target_conditions)을 새로 사용한다.
# ============================================================
import re
import json


def parse_target_conditions(response_text):
    """
    AI 응답에서 ```json ... ``` 코드블록만 안전하게 추출한다.
    반환: (실무자에게 보여줄 대화체 답변, 확정된 조건 dict)
    - JSON 블록이 없거나 파싱에 실패해도 예외를 던지지 않고 빈 조건을 반환한다.
    """
    if not response_text:
        return "", {}

    match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
    if not match:
        return response_text.strip(), {}

    reply_text = response_text[:match.start()].strip()
    try:
        conditions = json.loads(match.group(1))
        if not isinstance(conditions, dict):
            conditions = {}
    except json.JSONDecodeError:
        conditions = {}

    return reply_text, conditions


def format_push_copy_for_display(raw_text):
    """
    push_prompt 출력 형식([버전 N: ...] ■ 제목: ■ 내용:)을 그대로 신뢰하되,
    앞뒤 불필요한 공백/빈 줄만 정리해서 반환한다 (과도한 가공으로 원문 훼손 방지).
    """
    if not raw_text:
        return ""
    lines = [line.rstrip() for line in raw_text.strip().splitlines()]
    # 연속된 빈 줄은 하나로 축소
    cleaned = []
    prev_blank = False
    for line in lines:
        is_blank = (line.strip() == "")
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank
    return "\n".join(cleaned).strip()
