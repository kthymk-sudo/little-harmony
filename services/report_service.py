# services/report_service.py
# ============================================================
# 📝 보고서 탭 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
# AI는 업무 텍스트를 트리 요약+피드백으로 정리하고, 필요하면 데이터 피벗
# 스펙도 같이 내려준다. 실제 숫자는 📊 분석 탭과 똑같은 엔진
# (services/analysis_service.run_pivot_analysis)이 계산한다 - 두 탭이 서로
# 다른 계산 로직을 갖지 않도록 그대로 재사용.
# ============================================================
from ai_engine.gemini_api import generate_report_reply, generate_pivot_insight_reply, is_api_error
from database.db_manager import summarize_profile_context
from services.analysis_service import run_pivot_analysis, insight_spec_str, insight_rows_str, data_period_str
from utils.response_parser import parse_target_conditions


def process_report_turn(messages, user_text, profile_df, db_audience=None, db_content=None):
    """반환: (new_messages, chart_spec). chart_spec은 데이터 요청이 없었거나
    계산 결과가 비었으면 None."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = f"{summarize_profile_context(profile_df)}\n{data_period_str(db_audience)}"

    ai_raw = generate_report_reply(history_with_user, profile_context_str)
    reply_text, parsed = parse_target_conditions(ai_raw)
    parsed = parsed or {}

    data_spec = parsed.get('데이터요청') or {}
    chart_spec = None

    if data_spec.get('행') and data_spec.get('측정값'):
        pivot_result = run_pivot_analysis(db_audience, profile_df, data_spec, db_content)
        if pivot_result and pivot_result.get('결과'):
            chart_spec = {"type": "pivot", "data": pivot_result}
            data_insight = generate_pivot_insight_reply(
                user_text, insight_spec_str(data_spec, pivot_result), insight_rows_str(pivot_result),
            )
            if not is_api_error(data_insight):
                reply_text = f"{reply_text}\n\n📊 데이터 인사이트\n{data_insight}"
            else:
                reply_text = f"{reply_text}\n\n📊 데이터는 계산했지만 설명 생성에는 실패했어요. 아래 표/차트를 참고해주세요."

    new_message = {"role": "assistant", "text": reply_text}
    if chart_spec:
        new_message["chart"] = chart_spec
    new_messages = history_with_user + [new_message]
    return new_messages, chart_spec
