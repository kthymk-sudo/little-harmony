# services/intent_gate.py
# ============================================================
# 🌟 [요청 판단] 타겟팅·분석·보고서 세 기능이 대화를 받으면 본 작업 전에 AI가 먼저 뜻과 의도를 판단한다(prompts/intent_prompt.py).
#   진행   → 평소대로 본 작업(필요하면 "주의" 문구를 답 끝에 붙인다)
#   되묻기 → 뜻이 불분명하면 되묻는다(같은 작업을 되풀이하거나 오류를 내지 않는다)
#   안내   → 인사·잡담·기능 밖 질문은 짧게 답하고 할 수 있는 일을 안내한다
#   거절   → 불법·법령 위반 요청은 정중히 거절하고 합법적인 대안을 알려준다
# 판단이 안 되면(AI 오류·형식 오류) 진행으로 본다 - 판단 때문에 정상 업무가 막히지 않게 한다. 이 모듈은 Streamlit에 의존하지 않는다.
# ============================================================
import json
import re

from ai_engine.gemini_api import assess_request, is_api_error
from prompts.intent_prompt import get_intent_prompt

_PASS = {"판단": "진행", "작업": "", "답변": "", "주의": ""}
_VERDICTS = ("진행", "되묻기", "안내", "거절")


def _ask(prompt):   # 테스트에서 바꿔 끼운다
    return assess_request(prompt)


def assess(feature, user_text, last_reply="", state_line=""):
    """반환: {'판단': 진행|되묻기|안내|거절, '작업': '', '답변': '', '주의': ''}. 판단이 안 되면 진행."""
    if not (user_text or "").strip():
        return dict(_PASS)
    raw = _ask(get_intent_prompt(feature, user_text, last_reply, state_line))
    if is_api_error(raw):
        return dict(_PASS)
    block = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(block.group(0)) if block else {}
    except ValueError:
        return dict(_PASS)
    verdict = data.get("판단")
    if verdict not in _VERDICTS or (verdict != "진행" and not str(data.get("답변") or "").strip()):
        return dict(_PASS)   # 판단 형식이 틀렸거나, 막으면서 할 말이 없으면 막지 않는다
    return {k: str(data.get(k) or "").strip() for k in _PASS} | {"판단": verdict}


def with_notice(reply_text, verdict):
    """진행일 때 판단이 남긴 '주의' 문구를 답 끝에 붙인다."""
    note = verdict.get("주의")
    return f"{reply_text.rstrip()}\n\n※ 참고: {note}" if note else reply_text


def last_assistant_text(messages):
    return next((m.get("text", "") for m in reversed(messages) if m.get("role") == "assistant"), "")


if __name__ == "__main__":
    import sys

    me = sys.modules[__name__]
    sent = []
    me._ask = lambda p: sent.append(p) or '```json\n{"판단": "거절", "작업": "", "답변": "수신 동의 없는 광고 발송은 도와드릴 수 없어요. 동의 고객만 뽑아 드릴까요?", "주의": ""}\n```'
    v = assess("targeting", "수신 거부한 고객한테도 문자 보내자", "직전 답변", "조건 없음")
    assert v["판단"] == "거절" and "동의 고객" in v["답변"] and "수신 거부한 고객" in sent[0] and "직전 답변" in sent[0] and "타겟팅 & 카피" in sent[0]
    me._ask = lambda p: '{"판단": "진행", "작업": "자유", "답변": "", "주의": "광고성 문자는 (광고) 표기가 필요해요."}'
    v = assess("report", "메일 써줘")
    assert v["판단"] == "진행" and v["작업"] == "자유"
    assert with_notice("답이에요.", v) == "답이에요.\n\n※ 참고: 광고성 문자는 (광고) 표기가 필요해요." and with_notice("답", dict(_PASS)) == "답"
    # 판단이 안 되면 진행: API 오류, 형식 오류, 모르는 판단, 막으면서 할 말이 없는 경우, 빈 입력
    for bad in ("⚠️ 서버 오류", "그냥 글", '{"판단": "모름"}', '{"판단": "거절", "답변": ""}', '{깨진 json'):
        me._ask = lambda p, b=bad: b
        assert assess("analysis", "질문")["판단"] == "진행", bad
    me._ask = lambda p: sent.append("호출됨") or '{"판단": "거절", "답변": "x"}'
    n = len(sent)
    assert assess("analysis", "   ")["판단"] == "진행" and len(sent) == n, "빈 입력은 AI를 부르지 않는다"
    assert last_assistant_text([{"role": "assistant", "text": "a"}, {"role": "user", "text": "b"}]) == "a" and last_assistant_text([]) == ""
    # 기능별 안내문이 프롬프트에 들어가고, 보고서만 '작업' 선택 규칙이 있다
    assert "데이터 분석" in get_intent_prompt("analysis", "x", "", "") and "\"트리\"" not in get_intent_prompt("analysis", "x", "", "")
    assert "\"트리\"" in get_intent_prompt("report", "x", "", "")
    print("intent_gate self-check OK")
