# services/copy_service.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "카피 작성 대화 한 턴 처리" 순수 로직만 분리.
#
# 🌟 [버그 수정 - 실패 문구가 카피로 저장] AI 호출이 실패하면 "⚠️ ..." 안내 문구가 카피처럼
# 저장되고 작성 버튼도 비활성화돼 다시 만들기 어려웠다. 실패면 카피 없이(None) 돌려준다.
# 🌟 [글자수 확인] 제목/본문 글자수 제한을 AI가 넘겨도 알 수 없었다 - 버전별 글자수를 계산해
# 답변 아래에 붙인다(넘으면 ⚠️).
# ============================================================
import re
from ai_engine.gemini_api import generate_ai_push_copy, is_api_error
from prompts.target_agent_prompt import get_copy_agent_prompt
from services.agent_loop import run_agent, confused, contents_from
from config import PUSH_TITLE_MAX_LEN, PUSH_BODY_MAX_LEN, SMS_TITLE_MAX_LEN, SMS_BODY_MAX_LEN
from utils.response_parser import format_push_copy_for_display

COPY_TYPE_LABELS = {'push': '📱 앱푸시 카피', 'sms': '✉️ SMS 문자 카피'}
_LIMITS = {'push': (PUSH_TITLE_MAX_LEN, PUSH_BODY_MAX_LEN), 'sms': (SMS_TITLE_MAX_LEN, SMS_BODY_MAX_LEN)}
_VERSION = re.compile(r'\[버전\s*(\d+)[^\]]*\]\s*■\s*제목\s*:\s*(.*?)\s*■\s*내용\s*:\s*(.*?)(?=\n\s*\[버전|\Z)', re.S)


def length_report(copy_text, copy_type='push'):
    """버전별 제목/본문 글자수(공백 포함)와 제한 초과 여부. 형식을 못 읽으면 빈 문자열."""
    title_max, body_max = _LIMITS.get(copy_type, _LIMITS['push'])
    lines = []
    for number, title, body in _VERSION.findall(copy_text or ""):
        t_len, b_len = len(title.strip()), len(body.strip())
        over = t_len > title_max or b_len > body_max
        lines.append(f"{'⚠️' if over else '✅'} 버전 {number}: 제목 {t_len}/{title_max}자 · 본문 {b_len}/{body_max}자"
                     + (" - 제한 초과, 줄여달라고 요청해 주세요" if over else ""))
    return "\n".join(lines)


SHORTEN_REQUEST = "글자수 제한을 넘은 버전이 있어. 제목과 본문 모두 제한 안으로 줄여서 세 버전을 다시 써줘."


def is_over_limit(copy_text, copy_type='push'):
    """카피의 어느 한 버전이라도 제목/본문 글자수 제한을 넘었는지(버튼 표시 판단용)."""
    title_max, body_max = _LIMITS.get(copy_type, _LIMITS['push'])
    return any(len(t.strip()) > title_max or len(b.strip()) > body_max for _, t, b in _VERSION.findall(copy_text or ""))


_MAX_WRITES = 3   # 한 턴에서 카피를 다시 쓰게 되돌리는 최대 횟수(글자수 초과 등)
_COPY_DECLARATIONS = [{
    "name": "write_copy",
    "description": "실무자가 카피를 새로 쓰거나 바꿔 달라고 했을 때만 부른다(카피 세 버전을 새로 쓰거나 지금 카피를 요청대로 고쳐 다시 쓴다). 질문(\"왜 이렇게 썼어?\", \"어떤 타겟이야?\")에는 부르지 않는다. 결과로 카피와 글자수 확인이 온다.",
    "parameters": {"type": "OBJECT", "properties": {"instruction": {"type": "STRING", "description": "실무자가 원하는 방향(톤·소재·길이 등). 새로 쓰는 것이면 빈 문자열"}}},
}]
_CONFUSED = confused("'더 유머러스하게', '이벤트 느낌으로 바꿔줘'")


def _shown_copy(raw, copy_type):
    """AI가 쓴 카피를 화면용으로 갖춘다. 반환: (카피 글, 말풍선에 보일 글) 또는 실패하면 (None, 실패 문구)."""
    if is_api_error(raw):
        return None, raw
    copy_text = format_push_copy_for_display(raw)
    report = length_report(copy_text, copy_type)
    return copy_text, f"[{COPY_TYPE_LABELS.get(copy_type, COPY_TYPE_LABELS['push'])}]\n\n{copy_text}" + (f"\n\n📏 글자수 확인\n{report}" if report else "")


def _previous_copy(messages):
    """대화에서 가장 최근에 보여 준 카피 글(라벨과 글자수 확인 문단은 뺀다). 없으면 빈 문자열."""
    for m in reversed(messages):
        if m.get('role') == 'assistant' and m.get('text', '').startswith("[") and "■ 제목" in m['text']:
            return m['text'].split("\n\n", 1)[-1].split("\n\n📏")[0].strip()
    return ""


class _CopyTurn:
    def __init__(self, summary, reasoning, copy_type, previous):
        self.summary, self.reasoning, self.copy_type, self.previous = summary, reasoning, copy_type, previous
        self.copy_text = self.labeled = None
        self.writes = 0

    @property
    def worked(self):
        return self.copy_text is not None


def _tool_write_copy(turn, args):
    if turn.writes >= _MAX_WRITES:
        return {"오류": "이번 턴에는 더 쓰지 않아. 지금 카피를 보여 주고 어떻게 더 고칠지 실무자에게 물어봐."}
    turn.writes += 1
    instruction = str(args.get("instruction") or "").strip()
    base = turn.copy_text or turn.previous
    extra = f"[지금 카피 - 실무자가 바꿔 달라는 부분 외에는 이 카피의 방향을 이어가며 고쳐 써]\n{base}\n\n[요청]\n{instruction}" if base and instruction else instruction
    copy_text, labeled = _shown_copy(generate_ai_push_copy(turn.summary, turn.reasoning, extra, copy_type=turn.copy_type), turn.copy_type)
    if copy_text is None:
        return {"오류": labeled}
    turn.copy_text, turn.labeled = copy_text, labeled
    over = is_over_limit(copy_text, turn.copy_type) and turn.writes < _MAX_WRITES
    return {"카피": copy_text, "글자수": length_report(copy_text, turn.copy_type),
            "안내": "글자수를 넘은 버전이 있어. 한 번 더 불러서 제한 안으로 줄여 줘." if over
            else "카피는 화면에 그대로 보여 주니 본문을 다시 쓰지 말고 무엇을 어떻게 바꿨는지 한두 문장만 말해."}


def process_copy_turn(messages, target_summary_str, reasoning, user_text, copy_type='push'):
    """카피 작성 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
    copy_type: 'push'(앱푸시) 또는 'sms'(문자) - 어떤 형식/글자수 제약으로 만들지 결정.
    반환: (new_messages, copy_text) - 카피를 새로 쓰지 않았으면(실패·질문·거절·되묻기) copy_text는 None.
    🌟 첫 작성(user_text 빈 값)과 "글자수 맞춰 다시 쓰기" 버튼은 바로 쓴다. 실무자가 직접 입력한 말은 AI가 뜻을 읽어 카피를 고칠지(도구),
    질문에 답할지, 되물을지, 거절할지(허위·과장 광고, "(광고)" 표기·수신거부 안내 삭제 등) 스스로 정한다."""
    new_messages = messages + ([{"role": "user", "text": user_text}] if user_text else [])
    if not user_text or user_text == SHORTEN_REQUEST:
        copy_text, shown = _shown_copy(generate_ai_push_copy(target_summary_str, reasoning, user_text or "", copy_type=copy_type), copy_type)
        return new_messages + [{"role": "assistant", "text": shown}], copy_text
    label = COPY_TYPE_LABELS.get(copy_type, COPY_TYPE_LABELS['push'])
    turn = _CopyTurn(target_summary_str, reasoning, copy_type, _previous_copy(messages))
    state = f"카피 종류: {label}\n\n확정된 타겟 요약:\n{target_summary_str}\n\n타겟 선정 근거:\n{reasoning or '(없음)'}"
    text, api_error = run_agent(get_copy_agent_prompt(state, label), contents_from([('user' if m['role'] == 'user' else 'model', m['text']) for m in new_messages]), _COPY_DECLARATIONS,
                                {"write_copy": _tool_write_copy}, turn, temperature=0.3)
    if turn.copy_text is not None:
        return new_messages + [{"role": "assistant", "text": f"{text}\n\n{turn.labeled}" if text else turn.labeled}], turn.copy_text
    return new_messages + [{"role": "assistant", "text": api_error or text or _CONFUSED}], None


if __name__ == "__main__":
    import services.answer_check as _ac
    _ac.check_answer = lambda p: '{"문제": []}'   # 검수 AI는 테스트에서 부르지 않는다(문제 없음)
    import sys
    import services.agent_loop as loop
    me = sys.modules[__name__]
    sample = ("[버전 1: 호기심 유발형]\n■ 제목: 오늘 저녁엔 뭐 볼까요?\n■ 내용: 좋아하시는 트로트 무대가 새로 올라왔어요\n\n"
              "[버전 2: 혜택 강조형]\n■ 제목: 이번 주 새로 올라온 트로트 무대와 노래교실을 한 번에 모아보기\n■ 내용: 짧게\n")
    report = length_report(sample, 'push')
    assert report.splitlines()[0].startswith("✅ 버전 1: 제목 13/30자") and "⚠️ 버전 2" in report, report
    assert is_over_limit(sample, 'push') and not is_over_limit(sample.split("[버전 2")[0], 'push') and not is_over_limit("", 'push')
    written = []
    me.generate_ai_push_copy = lambda summary, reasoning, extra, copy_type='push': written.append(extra) or sample
    sent = []

    def script(*steps):
        it = iter(steps)

        def fake(system, contents, tool_declarations=None, temperature=0.3, force_tool=False):
            sent.append((system, list(contents), force_tool))
            step = next(it)
            if isinstance(step, tuple):
                return {"role": "model", "parts": [{"functionCall": {"name": step[0], "args": step[1]}}]}, None
            return {"role": "model", "parts": [{"text": step}]}, None
        loop.call_agent = fake

    # 첫 작성·글자수 줄이기 버튼은 AI 판단 없이 바로 쓰고, 실패는 카피로 저장하지 않는다
    loop.call_agent = lambda *a, **k: (_ for _ in ()).throw(AssertionError("버튼 요청은 대화 AI를 부르지 않는다"))
    msgs, copy = process_copy_turn([], "요약", "근거", "")
    assert copy == sample.strip() and msgs[-1]['text'].startswith("[📱 앱푸시 카피]") and '**' not in msgs[-1]['text'] and written[-1] == ""
    assert process_copy_turn([], "요약", "근거", SHORTEN_REQUEST)[1] == sample.strip()
    me.generate_ai_push_copy = lambda *a, **k: "⚠️ [서버 과부하] 잠시 후 다시"
    msgs, copy = process_copy_turn([], "요약", "근거", "")
    assert copy is None and msgs[-1]['text'].startswith("⚠️")
    me.generate_ai_push_copy = lambda summary, reasoning, extra, copy_type='push': written.append(extra) or sample
    # 수정 요청: AI가 write_copy를 불러 지금 카피를 바탕으로 고쳐 쓰고, 카피 뒤에 한 줄 설명이 붙는다. 글자수 초과면 한 번 더 줄이게 한다
    base = process_copy_turn([], "요약", "근거", "")[0]
    script(("write_copy", {"instruction": "더 유머러스하게"}), ("write_copy", {"instruction": "글자수 줄여서"}), "조금 더 장난스럽게 바꿨어요.")
    msgs, copy = process_copy_turn(base, "요약", "근거", "더 유머러스하게")
    assert copy == sample.strip() and msgs[-1]['text'].startswith("조금 더 장난스럽게") and "[📱 앱푸시 카피]" in msgs[-1]['text']
    assert "오늘 저녁엔 뭐 볼까요?" in written[-2] and "[요청]\n더 유머러스하게" in written[-2] and "📏" not in written[-2] and "[요청]\n글자수 줄여서" in written[-1]
    # 질문·거절·되묻기에는 카피를 바꾸지 않는다
    script("트로트를 좋아하는 분들이라 그렇게 썼어요.")
    msgs, copy = process_copy_turn(base, "요약", "근거", "왜 이렇게 썼어?")
    assert copy is None and msgs[-1]['text'].startswith("트로트를") and "근거" in sent[-1][0]
    script("(광고) 표기를 빼는 것은 도와드릴 수 없어요. 대신 짧고 눈에 띄는 제목으로 써 드릴까요?")
    msgs, copy = process_copy_turn(base, "요약", "근거", "(광고) 표기 빼고 써줘")
    assert copy is None and msgs[-1]['text'].startswith("(광고) 표기를 빼는")
    script("")
    assert process_copy_turn(base, "요약", "근거", "ㅇㅇ")[0][-1]['text'] == _CONFUSED
    print("copy_service self-check OK")
