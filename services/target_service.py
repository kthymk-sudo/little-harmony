# services/target_service.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "타겟 설정 대화 한 턴 처리" 순수 로직만 분리.
# Streamlit에 의존하지 않아 단위 테스트가 가능하다 (원래 코드 주석에 있던 설계
# 의도 그대로, 이제는 파일로도 분리되어 실제로 테스트하기 쉬워졌다).
# ============================================================
import json
from ai_engine.gemini_api import generate_target_chat_reply, generate_segment_insight_reply, is_api_error
from database.db_manager import (
    summarize_profile_context, summarize_segment_insight, format_segment_insight_reply,
    summarize_content_ranking, format_content_ranking_reply,
    summarize_group_breakdown, format_group_breakdown_reply,
    _normalize_field_name, describe_term_matches, describe_period_warnings, data_period_line,
)
from services.code_analyst import build_tables, run_analyst_turn
from utils.response_parser import parse_target_conditions
from utils.naver_search import search_term_meaning


def _answer_with_analyst(question, conditions, profile_df, db_audience, db_content):
    """🌟 [타겟팅 → 분석 연결] 정해진 질문 유형으로 답할 수 없는 데이터 질문은 분석 탭의 AI 코드 실행형 분석가가 실제 데이터를
    계산해 답한다. 지금 잡고 있는 타겟 조건은 참고로만 알려주고, 실무자가 그 집단을 말했을 때만 좁혀 보게 한다."""
    visible = {k: v for k, v in (conditions or {}).items() if not k.startswith('__')}
    if visible:
        question += f"\n(참고: 지금 대화 중인 타겟 조건은 {json.dumps(visible, ensure_ascii=False)}예요. 질문이 이 집단을 가리킬 때만 그 조건으로 좁혀 계산해.)"
    try:
        reply = run_analyst_turn([], question, build_tables(db_audience, db_content, profile_df))
    except Exception:  # 분석이 실패해도 타겟팅 대화는 계속되게
        return "분석 중 문제가 생겼어요. 데이터 분석 탭에서 같은 질문을 다시 해보시겠어요?"
    if is_api_error(reply['text']):
        return "분석 중 문제가 생겼어요. 잠시 뒤 다시 물어봐 주세요."
    return f"{reply['text']}\n\n(분석 탭과 같은 방식으로 데이터를 직접 계산한 결과예요. 계산 과정과 그래프는 데이터 분석 탭에서 같은 질문을 하면 볼 수 있어요.)"


def process_target_turn(messages, conditions, user_text, profile_df, db_audience=None, db_content=None):
    """타겟 설정 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능)."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df) + "\n" + data_period_line(db_audience)
    # '__' 접두사 키(분석 탭에서 가져온 고객번호 목록 등)는 내부용이라 AI에게는 보내지 않는다
    visible_conditions = {k: v for k, v in (conditions or {}).items() if not k.startswith('__')}
    conditions_str = json.dumps(visible_conditions, ensure_ascii=False)

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

    # 🌟 [시청기록 자유 분석] "가장 인기있는 콘텐츠는?"(콘텐츠채널순위질문), "성별로 분포가
    # 어때?"(그룹현황질문)도 "질문조건"과 완전히 같은 이유로 - 확정 타겟과 섞이면 안 되고
    # 매 턴 새로 판단되는 1회성 값이므로 - 병합 루프 전에 따로 떼어낸다.
    content_ranking_question = parsed_conditions.pop('콘텐츠채널순위질문', None)
    group_breakdown_question = parsed_conditions.pop('그룹현황질문', None)
    analysis_question = parsed_conditions.pop('분석질문', None)  # 위 유형으로 답할 수 없는 자유 분석 질문(1회성)
    analysis_question = analysis_question.strip() if isinstance(analysis_question, str) else ""

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
    # 🌟 [버그 수정 - 필드명 불일치] AI가 "나이대" 대신 "나이"/"연령대"처럼 스키마와
    # 살짝 다른 이름을 쓰면 정확 일치 pop()은 조용히 아무 일도 안 해서 "빼달라고
    # 했는데 안 빠졌다"로 보였다. _normalize_field_name()으로 정식 필드명으로
    # 바꾼 뒤 지운다 (어떤 필드로도 매핑되지 않으면 무시 - 무리하게 아무 필드나
    # 지우는 것보다 안전).
    for field in fields_to_delete:
        canonical_field = _normalize_field_name(field)
        if canonical_field:
            merged_conditions.pop(canonical_field, None)

    if isinstance(question_conditions, dict) and question_conditions:
        insight = summarize_segment_insight(profile_df, question_conditions, db_audience)
        # 🌟 [DB 질문응답 고도화] 여태까지는 실제 계산(insight)까지는 잘 해놓고도,
        # 그 결과를 프롬프트(get_segment_insight_prompt)로 AI에게 넘겨 자연스러운
        # 문장으로 답하게 하는 generate_segment_insight_reply()가 어디에서도 호출되지
        # 않고 있었다 - 대신 항상 format_segment_insight_reply()라는 고정된 파이썬
        # 문자열 템플릿("~명이에요. ~순으로 많이 봤어요. 타겟을 잡아볼까요?")만 써서,
        # 실무자가 어떻게 물어봤든 늘 똑같은 기계적인 문장이 나갔다. 이제는 계산된
        # 결과가 있을 때(대상자수 > 0)는 실제로 AI를 한 번 더 호출해서, 실무자가
        # 물어본 질문 자체(user_text)에 맞춰 자연스럽게 답하게 한다. 데이터가 없거나
        # (0명) AI 호출이 실패하면(is_api_error) 기존의 확정적인 템플릿 문장으로
        # 안전하게 되돌아간다 - 이 프롬프트도 "실제 계산된 수치만 근거로 답해"라고
        # 못박아 두었으므로(get_segment_insight_prompt), 숫자를 지어낼 위험은 없다.
        fallback_reply = format_segment_insight_reply(insight)
        if insight and insight.get('대상자수', 0) > 0:
            ai_insight_reply = generate_segment_insight_reply(
                user_text,
                json.dumps(question_conditions, ensure_ascii=False),
                json.dumps(insight, ensure_ascii=False),
            )
            reply_text = fallback_reply if is_api_error(ai_insight_reply) else ai_insight_reply
        else:
            reply_text = fallback_reply

    # 🌟 [시청기록 자유 분석] "질문조건"과 같은 패턴: 실제 계산은 database.audience의
    # 결정론적 집계 함수(summarize_content_ranking/summarize_group_breakdown)가 하고,
    # AI는 그 결과를 자연스러운 문장으로 바꾸는 역할만 한다(is_api_error 시 고정
    # 템플릿으로 폴백). AI가 순위/숫자를 직접 지어내는 경로는 없다.
    elif isinstance(content_ranking_question, dict) and content_ranking_question.get('대상') and content_ranking_question.get('기준'):
        ranking = summarize_content_ranking(
            db_audience,
            profile_df=profile_df,
            conditions=content_ranking_question.get('필터조건') or None,
            target=content_ranking_question.get('대상'),
            order=content_ranking_question.get('기준'),
        )
        fallback_reply = format_content_ranking_reply(ranking)
        if ranking and ranking.get('항목'):
            ai_ranking_reply = generate_segment_insight_reply(
                user_text,
                json.dumps(content_ranking_question, ensure_ascii=False),
                json.dumps(ranking, ensure_ascii=False),
            )
            reply_text = fallback_reply if is_api_error(ai_ranking_reply) else ai_ranking_reply
        else:
            reply_text = fallback_reply

    elif isinstance(group_breakdown_question, dict) and group_breakdown_question.get('기준필드'):
        breakdown = summarize_group_breakdown(
            profile_df,
            group_breakdown_question.get('기준필드'),
            conditions=group_breakdown_question.get('필터조건') or None,
            db_audience=db_audience,
        )
        fallback_reply = format_group_breakdown_reply(breakdown)
        if breakdown and breakdown.get('그룹'):
            ai_breakdown_reply = generate_segment_insight_reply(
                user_text,
                json.dumps(group_breakdown_question, ensure_ascii=False),
                json.dumps(breakdown, ensure_ascii=False),
            )
            reply_text = fallback_reply if is_api_error(ai_breakdown_reply) else ai_breakdown_reply
        else:
            reply_text = fallback_reply

    elif analysis_question:
        reply_text = _answer_with_analyst(analysis_question, conditions, profile_df, db_audience, db_content)

    # 🌟 [키워드 매칭 안내] 이번 턴에 새로 나온 장르/채널/메뉴 조건이 실제 데이터의 어떤 값에 걸렸는지 알려준다
    # (질문 답변 턴이 아니라 조건을 정하는 턴에서만 - 조건이 그대로면 매번 반복하지 않는다)
    is_answer_turn = bool(
        (isinstance(question_conditions, dict) and question_conditions)
        or (isinstance(content_ranking_question, dict) and content_ranking_question.get('대상') and content_ranking_question.get('기준'))
        or (isinstance(group_breakdown_question, dict) and group_breakdown_question.get('기준필드'))
        or analysis_question
    )  # (빈 틀만 채워 온 {"대상": "", ...}는 질문이 아니다)
    if not is_answer_turn:
        new_only = {k: v for k, v in merged_conditions.items() if (conditions or {}).get(k) != v}
        # 같은 검색어가 선호장르와 기간내시청 장르포함에 함께 있으면 안내가 두 번 나오므로 한 번만 남긴다
        notes = list(dict.fromkeys(describe_period_warnings(db_audience, new_only) + describe_term_matches(db_audience, new_only)))
        if notes:
            reply_text = reply_text.rstrip() + "\n\n[참고]\n· " + "\n· ".join(notes)

    new_messages = history_with_user + [{"role": "assistant", "text": reply_text}]
    return new_messages, merged_conditions


if __name__ == "__main__":
    # 정해진 질문 유형으로 답할 수 없는 데이터 질문은 분석가에게 넘겨 답하고, 타겟 조건은 그대로 두며, 분석이 실패해도 대화는 계속된다
    tail = '\n```json\n{"분석질문": "6월 재방문율은?", "질문조건": {}}\n```'
    globals()['generate_target_chat_reply'] = lambda *a, **k: "분석해볼게요." + tail
    globals()['summarize_profile_context'] = lambda p: ""
    globals()['build_tables'] = lambda *a: {}
    asked = []
    globals()['run_analyst_turn'] = lambda msgs, q, tables: asked.append(q) or {'role': 'assistant', 'text': '재방문율은 40%예요.'}
    msgs, cond = process_target_turn([], {"성별": "여자"}, "6월 재방문율은?", None)
    assert "재방문율은 40%" in msgs[-1]['text'] and cond == {"성별": "여자"} and "[참고]" not in msgs[-1]['text'], msgs[-1]
    assert asked[0].startswith("6월 재방문율은?") and "성별" in asked[0]

    def _boom(*a):
        raise RuntimeError("x")
    globals()['run_analyst_turn'] = _boom
    assert "분석 중 문제" in process_target_turn([], {}, "x", None)[0][-1]['text']
    print("target_service self-check OK")
