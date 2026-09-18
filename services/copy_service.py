# services/copy_service.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "카피 작성 대화 한 턴 처리" 순수 로직만 분리.
# ============================================================
from ai_engine.gemini_api import generate_ai_push_copy
from utils.response_parser import format_push_copy_for_display

COPY_TYPE_LABELS = {'push': '📱 앱푸시 카피', 'sms': '✉️ SMS 문자 카피'}


def process_copy_turn(messages, target_summary_str, reasoning, user_text, copy_type='push'):
    """카피 작성 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
    copy_type: 'push'(앱푸시) 또는 'sms'(문자) - 어떤 형식/글자수 제약으로 만들지 결정."""
    raw = generate_ai_push_copy(target_summary_str, reasoning, user_text or "", copy_type=copy_type)
    copy_text = format_push_copy_for_display(raw)
    label = COPY_TYPE_LABELS.get(copy_type, COPY_TYPE_LABELS['push'])
    labeled_text = f"**[{label}]**\n\n{copy_text}"
    new_messages = messages + ([{"role": "user", "text": user_text}] if user_text else [])
    new_messages = new_messages + [{"role": "assistant", "text": labeled_text}]
    return new_messages, copy_text
