# services/agent_loop.py
# ============================================================
# 🌟 [도구를 쓰는 AI 대화의 공통 반복] 보고서·타겟팅 탭이 함께 쓴다(Streamlit 비의존).
# 모델에 시스템 지침 + 대화를 보내고, 모델이 도구를 부르면 실행해서 결과를 돌려주는 일을 글 답이 나올 때까지 반복한다.
# 🌟 [거짓 완료 방지] 모델이 도구를 안 부르고 글로만 "했어요"라고 하거나 도구 호출을 글로 흉내 내면(tool_call) 그 답을 버리고,
# 이번에는 도구를 반드시 부르게 해서 다시 시킨다.
# ============================================================
import re

from ai_engine.gemini_api import call_agent

MAX_ROUNDS = 8       # 한 턴에서 도구를 부르고 결과를 돌려주는 최대 횟수
_MAX_NUDGES = 2      # 도구를 부르지 않고 "했어요"라고만 답했을 때 되돌려 보내는 최대 횟수
_DONE_CLAIM = re.compile(r"저장(했|해 ?[두뒀]|됐|되었)|만들었|만들어 ?[두뒀]|정리(했|해 ?[두뒀])|추가했|반영했|담았|바꿨|수정했|뒤집었|요약했|합쳤"
                         r"|설정했|적용했|뺐|넣었|작성했|써 ?[두뒀]|다시 썼|계산했")   # 일을 마쳤다는 말(거짓말 감지용)
_TOOL_TEXT = "tool_call"   # 모델이 도구 호출을 글로 흉내 낸 흔적
_NUDGE = ("(시스템 알림) 방금 답에서 작업을 마쳤다고 했지만 실제로는 도구를 부르지 않아서 아무것도 이뤄지지 않았어. 해야 할 일이면 지금 도구를 불러서 실제로 하고, "
          "그 일이 필요 없었다면 한 일이 없다는 사실에 맞게 다시 답해. 이 알림은 실무자에게 말하지 마.")
_ERROR = {"오류": "처리 중 문제가 생겼어. 다시 시도하거나 사람 말로 사정을 설명해 줘."}


def run_agent(system, contents, declarations, tools, turn, temperature=0.2):
    """contents(대화)에 모델 응답과 도구 결과를 이어 붙이며 반복한다. tools: {이름: 함수(turn, args) -> dict}, turn.worked: 이번 턴에 도구가 실제로 일을 했는지.
    반환: (마지막 글 답, API 오류 문구). 도구만 계속 부르다 끝나면 글 답은 빈 문자열이다."""
    nudges, force = 0, False
    for _ in range(MAX_ROUNDS):
        content, err = call_agent(system, contents, declarations, temperature=temperature, force_tool=force)
        force = False
        if err:
            return "", err
        contents.append(content)
        calls = [p['functionCall'] for p in content['parts'] if 'functionCall' in p]
        if not calls:
            said = "".join(p.get('text', '') for p in content['parts'])
            if nudges < _MAX_NUDGES and (_TOOL_TEXT in said or (_DONE_CLAIM.search(said) and not turn.worked)):
                nudges += 1   # 도구를 부르지 않고 글로만 "했다"고 한 답은 버리고, 이번에는 도구를 반드시 부르게 한다
                contents.pop()
                contents[-1]["parts"].append({"text": _NUDGE})
                force = True
                continue
            return "".join(p.get('text', '') for p in content['parts']
                           if 'functionCall' not in p and not p.get('thought') and _TOOL_TEXT not in p.get('text', '')).strip(), ""
        results = []
        for call in calls:
            tool = tools.get(call.get('name'))
            try:
                result = tool(turn, call.get('args') or {}) if tool else {"오류": "그런 기능은 없어."}
            except Exception:
                result = _ERROR
            results.append({"functionResponse": {"name": call.get('name'), "response": result}})
        contents.append({"role": "user", "parts": results})
    return "", ""


def confused(example):
    """뜻을 못 알아들었을 때(AI가 빈 답을 낸 경우) 사람 말로 되묻는 문구. example: 이런 식으로 말해 달라는 예."""
    return f"요청하신 뜻을 정확히 이해하지 못했어요. 어떤 일을 도와드리면 될지 한 번만 더 말씀해 주시겠어요? (예: {example})"


def contents_from(pairs):
    """[(역할 'user'|'model', 글)]을 AI에게 보낼 대화로: 글이 빈 것은 빼고, 같은 역할이 이어지면 합치고, 첫 줄은 사용자 말이어야 한다."""
    out = []
    for role, text in pairs:
        if not text.strip():
            continue
        if out and out[-1]['role'] == role:
            out[-1]['parts'][0]['text'] += "\n\n" + text
        else:
            out.append({"role": role, "parts": [{"text": text}]})
    while out and out[0]['role'] != 'user':
        out.pop(0)
    return out
