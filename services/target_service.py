# services/target_service.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "타겟 설정 대화 한 턴 처리" 순수 로직만 분리.
# Streamlit에 의존하지 않아 단위 테스트가 가능하다 (원래 코드 주석에 있던 설계
# 의도 그대로, 이제는 파일로도 분리되어 실제로 테스트하기 쉬워졌다).
# ============================================================
import json
from ai_engine.gemini_api import generate_target_chat_reply
from database.db_manager import summarize_profile_context, summarize_segment_insight, format_segment_insight_reply
from utils.response_parser import parse_target_conditions
from utils.naver_search import search_term_meaning


def process_target_turn(messages, conditions, user_text, profile_df, db_audience=None):
    """타겟 설정 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능)."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df)
    conditions_str = json.dumps(conditions or {}, ensure_ascii=False)

    ai_raw = generate_target_chat_reply(history_with_user, profile_context_str, conditions_str)
    reply_text, parsed_conditions = parse_target_conditions(ai_raw)
    parsed_conditions = parsed_conditions or {}

    # 🌟 [신조어/속어 이해 고도화] "꽃중년"처럼 AI가 뜻을 확신하지 못하는 표현은 추측하지
    # 않고 "확인필요단어"에만 담아 돌려주도록 프롬프트에서 유도했다. 이런 단어가 있으면
    # (설정되어 있다면) 검색으로 실제 뜻을 확인한 뒤, 그 뜻을 프롬프트에 더해 같은 턴을
    # 한 번 더 해석시킨다. 🌟 [2026-09 기준] 검색 연동 자체(utils/naver_search.py)는
    # 당분간 보류하기로 해서 API 키가 없는 상태 - 이 경우 search_term_meaning()이 항상
    # None을 반환하므로 아래 if는 통과되지 않고, 1차 응답에서 AI가 직접 실무자에게
    # 되물은 질문이 그대로 답변으로 나간다(무리하게 추측하는 것보다 안전). 추후 검색
    # 연동을 켜면 코드 변경 없이 자동으로 이 2차 재해석 경로가 활성화된다.
    unclear_terms = parsed_conditions.pop('확인필요단어', None) or []
    if unclear_terms:
        term_defs = []
        for term in unclear_terms[:3]:  # 한 턴에 걸리는 검색 호출 수 상한
            snippet = search_term_meaning(term)
            if snippet:
                term_defs.append(f"[{term}]\n{snippet}")
        if term_defs:
            term_context_str = "\n\n".join(term_defs)
            ai_raw2 = generate_target_chat_reply(
                history_with_user, profile_context_str, conditions_str, term_context_str,
            )
            reply_text2, parsed_conditions2 = parse_target_conditions(ai_raw2)
            if parsed_conditions2:
                parsed_conditions2.pop('확인필요단어', None)
                reply_text, parsed_conditions = reply_text2, parsed_conditions2

    # 🌟 [탐색형 대화 고도화] "질문조건"은 확정 타겟이 아니라, "이 세그먼트는 뭘 가장
    # 많이 봐?"처럼 실무자가 순수하게 궁금해서 물어본 세그먼트를 계산하기 위한 1회성
    # 값이다. 아래 조건 병합 루프에 절대 섞이면 안 되므로(섞이면 질문 한 번에 확정
    # 타겟이 의도치 않게 바뀐다) 먼저 따로 떼어낸다.
    question_conditions = parsed_conditions.pop('질문조건', None)

    # 🌟 [조건 삭제 지원] "나이 조건 빼줘"처럼 실무자가 명확히 삭제를 요청하면, AI가
    # 해당 필드명을 이 리스트에 담아 내려준다. 아래 빈 값 스킵 병합 로직과는 별개로,
    # 병합이 끝난 뒤 이 리스트에 있는 필드만 확실하게 지워준다.
    fields_to_delete = parsed_conditions.pop('삭제할조건', None) or []

    merged_conditions = dict(conditions or {})
    for k, v in (parsed_conditions or {}).items():
        # 🌟 [타겟팅 고도화] 제외조건은 dict 값이라 "v not in (None, [], '')" 비교로는
        # 빈 dict({})를 걸러낼 수 없다(빈 dict는 저 셋 중 무엇과도 == 이 아니므로 그대로
        # 통과되어, AI가 매턴 빈 제외조건을 같이 내려줄 때마다 이전에 실무자가 확정해둔
        # 제외조건을 실수로 지워버리는 문제가 생긴다). dict는 "비어있지 않을 때만" 병합.
        if isinstance(v, dict):
            if v:
                merged_conditions[k] = v
        elif v not in (None, [], ""):
            merged_conditions[k] = v

    # 🌟 [조건 삭제 지원] 위 병합 로직은 "빈 값=언급 안 함(유지)"으로 취급하므로, 실제
    # 삭제는 AI가 명시적으로 알려준 필드에 한해 여기서 최종적으로 한 번 더 지워준다.
    for field in fields_to_delete:
        merged_conditions.pop(field, None)

    if isinstance(question_conditions, dict) and question_conditions:
        insight = summarize_segment_insight(profile_df, question_conditions, db_audience)
        reply_text = format_segment_insight_reply(insight)

    new_messages = history_with_user + [{"role": "assistant", "text": reply_text}]
    return new_messages, merged_conditions
