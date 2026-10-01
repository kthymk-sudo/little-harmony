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
from services.intent_gate import assess, with_notice, last_assistant_text
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


def process_copy_turn(messages, target_summary_str, reasoning, user_text, copy_type='push'):
    """카피 작성 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
    copy_type: 'push'(앱푸시) 또는 'sms'(문자) - 어떤 형식/글자수 제약으로 만들지 결정.
    반환: (new_messages, copy_text) - AI 호출이 실패하면 copy_text는 None.
    🌟 실무자가 직접 쓴 수정 요청(user_text)은 먼저 뜻·의도를 판단한다: 불분명하면 되묻고, 불법(허위·과장 광고, "(광고)" 표기·수신거부 안내 삭제 등)이면
    거절하고, 그때는 카피를 바꾸지 않는다(copy_text=None)."""
    new_messages = messages + ([{"role": "user", "text": user_text}] if user_text else [])
    verdict = {"판단": "진행", "주의": ""}
    if user_text and user_text != SHORTEN_REQUEST:
        verdict = assess('targeting', user_text, last_assistant_text(messages), f"{'문자(SMS)' if copy_type == 'sms' else '앱푸시'} 카피를 작성·수정하는 중")
        if verdict['판단'] != '진행':
            return new_messages + [{"role": "assistant", "text": verdict['답변']}], None
    raw = generate_ai_push_copy(target_summary_str, reasoning, user_text or "", copy_type=copy_type)
    if is_api_error(raw):
        return new_messages + [{"role": "assistant", "text": raw}], None
    copy_text = format_push_copy_for_display(raw)
    label = COPY_TYPE_LABELS.get(copy_type, COPY_TYPE_LABELS['push'])
    report = length_report(copy_text, copy_type)
    labeled_text = f"[{label}]\n\n{copy_text}" + (f"\n\n📏 글자수 확인\n{report}" if report else "")
    return new_messages + [{"role": "assistant", "text": with_notice(labeled_text, verdict)}], copy_text


if __name__ == "__main__":
    sample = ("[버전 1: 호기심 유발형]\n■ 제목: 오늘 저녁엔 뭐 볼까요?\n■ 내용: 좋아하시는 트로트 무대가 새로 올라왔어요\n\n"
              "[버전 2: 혜택 강조형]\n■ 제목: 이번 주 새로 올라온 트로트 무대와 노래교실을 한 번에 모아보기\n■ 내용: 짧게\n")
    report = length_report(sample, 'push')
    assert report.splitlines()[0].startswith("✅ 버전 1: 제목 13/30자") and "⚠️ 버전 2" in report, report
    import services.intent_gate as _g0
    _g0._ask = lambda p: '{"판단": "진행"}'
    globals()['generate_ai_push_copy'] = lambda *a, **k: "⚠️ [서버 과부하] 잠시 후 다시"
    msgs, copy = process_copy_turn([], "요약", "근거", "")
    assert copy is None and msgs[-1]['text'].startswith("⚠️"), "실패는 카피로 저장하지 않는다"
    globals()['generate_ai_push_copy'] = lambda *a, **k: sample
    msgs, copy = process_copy_turn([], "요약", "근거", "더 짧게")
    assert copy == sample.strip() and msgs[-1]['text'].startswith("[📱 앱푸시 카피]") and '**' not in msgs[-1]['text']
    assert is_over_limit(sample, 'push') and not is_over_limit(sample.split("[버전 2")[0], 'push') and not is_over_limit("", 'push')
    # 요청 판단: 불법 요청은 카피를 바꾸지 않고 거절만 한다
    import services.intent_gate as _gate
    _gate._ask = lambda p: '{"판단": "거절", "답변": "(광고) 표기를 빼는 것은 도와드릴 수 없어요. 대신 짧고 눈에 띄는 제목으로 다시 써 드릴까요?"}'
    msgs, copy = process_copy_turn([], "요약", "근거", "(광고) 표기 빼고 써줘")
    assert copy is None and msgs[-1]['text'].startswith("(광고) 표기를 빼는 것은") and msgs[0]['text'] == "(광고) 표기 빼고 써줘"
    _gate._ask = lambda p: (_ for _ in ()).throw(AssertionError("글자수 줄이기 요청은 판단하지 않는다"))
    assert process_copy_turn([], "요약", "근거", SHORTEN_REQUEST)[1] == sample.strip()
    print("copy_service self-check OK")
