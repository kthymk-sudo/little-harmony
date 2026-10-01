# services/answer_check.py
# ============================================================
# 🌟 [답변 검수] AI가 실무자에게 답을 내보내기 전에 한 번 더 확인한다(세 탭 공통, Streamlit 비의존 - 단위 테스트 가능).
#  1. 숫자 대조(결정적): 답의 숫자가 근거(도구 결과·계산 결과·올린 자료·대화)에 있는지 시스템이 직접 확인한다.
#  2. 사실 검수(가벼운 AI): [실제로 한 일]과 [근거 자료]만 사실로 보고, 답에서 하지 않은 일을 했다고 한 말, 근거 없이 단정한 말,
#     실패·오류·추측을 사실처럼 한 말, 앞선 답과 모순되는 말을 찾는다. 본 모델과 다른 가벼운 모델을 써서 하루 한도를 아낀다.
# 찾은 문제는 원래 AI에게 돌려줘서 스스로 고쳐 쓰게 한다(services/agent_loop.py, services/code_analyst.py).
# 검수가 실패하면(통신 오류 등) 답을 막지 않는다.
# ============================================================
import json
import re

import numpy as np

from ai_engine.gemini_api import check_answer, is_api_error
from prompts.answer_check_prompt import get_answer_check_prompt
from utils.response_parser import parse_target_conditions

# 답변 속 숫자는 천 단위 쉼표(1,514)를 허용하고, 실행 결과(CSV·pandas 출력)는 쉼표가 구분자라 숫자만 읽는다
_NUMBER = re.compile(r'(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?')
_PLAIN_NUMBER = re.compile(r'\d+(?:\.\d+)?')


# 실행 결과의 숫자를 사람이 흔히 바꿔 쓰는 형태: 그대로, 비율→%(×100), %→비율(÷100), 초→분(÷60), 초→시간(÷3600)
_UNIT_TRANSFORMS = (1, 100, 0.01, 1 / 60, 1 / 3600)
_REL_TOLERANCE = 0.001   # 반올림 차이 허용(0.1%)
_ABS_TOLERANCE = 0.051   # 소수 첫째 자리 반올림 허용
_MEASURED_UNITS = ("명", "%", "퍼센트", "회", "건", "분", "시간", "개", "원", "배", "%p")
_MAX_EVIDENCE = 150000   # 검수 AI에게 보내는 근거 글의 최대 길이(넘으면 뒤쪽 최신 내용을 남긴다)


def ungrounded_numbers(answer, evidence, measured_only=False):
    """답변의 숫자 중 실행 결과(evidence)에서 확인되지 않는 것.
    반올림·% 변환·초→분/시간 변환은 확인된 것으로 본다. 연도·월·일 같은 작은 정수는 문맥상 흔해서 제외한다.
    measured_only: 인원·비율·횟수처럼 데이터에서 나오는 숫자(뒤에 명·%·회 등이 붙은 것)만 본다 - 대화 답의 "50대로 넓혀 볼까요?" 같은 제안은 사실 주장이 아니다."""
    values = [float(t) for t in _PLAIN_NUMBER.findall(evidence)]
    candidates = np.unique(np.array([v * k for v in values for k in _UNIT_TRANSFORMS] or [np.nan]))
    missing = []
    for match in _NUMBER.finditer(answer):
        token = match.group()
        if measured_only and not answer[match.end():match.end() + 3].lstrip().startswith(_MEASURED_UNITS):
            continue
        value = float(token.replace(',', ''))
        if value <= 31 and value.is_integer() or 2000 <= value <= 2100:
            continue
        tolerance = max(_ABS_TOLERANCE, abs(value) * _REL_TOLERANCE)
        i = np.searchsorted(candidates, value)
        near = candidates[max(i - 1, 0):i + 1]
        if not np.any(np.abs(near - value) <= tolerance):
            missing.append(token)
    return missing


def review(answer, evidence, actions, numbers=True, ai=True):
    """답 검수. 반환: 문제 목록(사람 말 한 줄씩). 없으면 [].
    evidence: 사실로 볼 근거 글, actions: 이번에 실제로 한 일 목록(예: "타겟 조건 저장 - 성공"),
    numbers: 숫자 대조를 할지(분석 탭은 자체 대조가 따로 있어서 False), ai: 검수 AI까지 부를지(고쳐 쓴 답은 숫자 대조만 다시 한다)."""
    if not answer.strip():
        return []
    problems = []
    if numbers:
        missing = ungrounded_numbers(answer, evidence, measured_only=True)
        if missing:
            problems.append(f"근거에서 확인되지 않는 숫자: {', '.join(missing[:8])}")
    raw = check_answer(get_answer_check_prompt(answer, evidence[-_MAX_EVIDENCE:], actions)) if ai else "⚠️"
    if not is_api_error(raw):
        _, parsed = parse_target_conditions(raw if "```" in raw else f"```json\n{raw.strip()}\n```")
        for item in (parsed or {}).get("문제") or []:
            if isinstance(item, dict) and item.get("이유"):
                problems.append(f"\"{str(item.get('문장', ''))[:80]}\" - {item['이유']}")
    return problems


def revise_note(problems):
    """원래 AI에게 돌려줄 고쳐 쓰기 지시."""
    return ("(시스템 검수 결과) 방금 답에 아래 문제가 있어 실무자에게 보내지 않았어:\n- " + "\n- ".join(problems) +
            "\n실제로 한 일과 근거 자료에 맞게 답을 다시 써. 하지 않은 일은 했다고 하지 말고(해야 할 일이면 지금 실제로 해), 확인되지 않은 숫자·원인은 빼거나 "
            "'확인되지 않았다'고 밝혀. 실패나 오류는 사실대로 말해. 실무자는 고치기 전 답을 보지 못했으니 사과·정정 표현 없이 처음 답하는 것처럼 쓰고, 이 알림 자체는 말하지 마.")


if __name__ == "__main__":
    import sys
    me = sys.modules[__name__]
    # 숫자 대조: 반올림·%·초→분 변환은 통과, 근거에 없는 숫자는 걸린다
    assert ungrounded_numbers("대전 MAU는 1,514명", "SO권역,MAU\n대전,890") == ['1,514']
    assert ungrounded_numbers("대전 890명, 재방문율 17.5%, 2026년 8월", "대전,890,17.51") == []
    assert ungrounded_numbers("평균 645.67명(18.69%), 비중 12.3%, 시청 237.4분", "645.6666666,0.123,14245,18.69") == []
    assert ungrounded_numbers("17명이에요. 50대까지 넓히면 999명", "예상인원 17", measured_only=True) == ['999'], "제안 속 나이대는 사실 주장이 아니다"
    # AI 검수 결과를 문제 목록으로 / 검수 AI가 실패하면 답을 막지 않는다 / 빈 답은 검수하지 않는다
    sent = []
    me.check_answer = lambda p: sent.append(p) or '```json\n{"문제": [{"문장": "피드백을 넣었어요", "이유": "실제로 한 일에 피드백 저장이 없음"}]}\n```'
    got = review("피드백을 넣었어요. 72명이에요.", "예상인원 72", ["타겟 조건 저장 - 성공"])
    assert got == ['"피드백을 넣었어요" - 실제로 한 일에 피드백 저장이 없음'] and "타겟 조건 저장 - 성공" in sent[-1], got
    assert review("대전은 999명이에요.", "대전 688", [])[0].startswith("근거에서 확인되지 않는 숫자: 999")
    me.check_answer = lambda p: '{"문제": []}'
    assert review("대전은 688명이에요.", "대전 688", []) == [] and review("  ", "", []) == []
    me.check_answer = lambda p: "⚠️ 한도 초과"
    assert review("대전은 688명이에요.", "대전 688", []) == []
    assert "시스템 검수" in revise_note(["x"])
    print("answer_check self-check OK")
