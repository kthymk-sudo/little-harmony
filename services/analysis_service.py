# services/analysis_service.py
# ============================================================
# 📊 분석 탭 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
# 고정된 질문 유형(그룹현황/순위) 대신, AI가 번역한 피벗 스펙(행/열/측정값/
# 집계방식/차트유형)을 받아 시청이력 원본(db_audience, 리스트 컬럼이 없는
# 평평한 이벤트 단위 데이터)을 직접 피벗 집계한다 - 구 시스템
# visualization/chart_generator.py의 피벗 로직을 Streamlit 의존 없이 이식.
# AI는 숫자를 전혀 만들지 않는다: 행/열/측정값을 해석만 하고, 실제 집계는
# 전부 이 파일의 결정론적 pandas 연산이 담당한다.
# ============================================================
import pandas as pd
from ai_engine.gemini_api import generate_analysis_chat_reply, generate_segment_insight_reply, is_api_error
from database.db_manager import summarize_profile_context, _filter_by_conditions
from utils.response_parser import parse_target_conditions

# 행/열로 쓸 수 있는 필드 -> 실제 db_audience 컬럼명 (다르면 매핑, 같으면 자기 자신)
_FIELD_COLUMN_MAP = {'SO': '시청자SO'}
_ALLOWED_ROW_COL_FIELDS = {'성별', '나이대', 'SO', '채널명', '메뉴명', '장르', '영상명', '시청시간대', '월', '시청일'}

# 측정값 -> {집계에 쓸 컬럼, 허용 집계방식(AI 표현 -> pandas aggfunc), 표시 단위/나누기}
_VALUE_SPECS = {
    '시청시간': {'col': '시청시간', 'aggs': {'합계': 'sum', '평균': 'mean'}, 'default_agg': '합계', 'divide': 60, 'unit': '분'},
    '시청유지율': {'col': '시청 유지율', 'aggs': {'평균': 'mean'}, 'default_agg': '평균', 'divide': 1, 'unit': '%'},
    '시청자수': {'col': 'R고객번호', 'aggs': {'고유값수': 'nunique'}, 'default_agg': '고유값수', 'divide': 1, 'unit': '명'},
    '시청건수': {'col': 'R고객번호', 'aggs': {'개수': 'count'}, 'default_agg': '개수', 'divide': 1, 'unit': '건'},
    '콘텐츠수': {'col': '콘텐츠ID', 'aggs': {'고유값수': 'nunique'}, 'default_agg': '고유값수', 'divide': 1, 'unit': '개'},
}


def _add_derived_columns(df):
    df = df.copy()
    if '나이' in df.columns:
        age_num = pd.to_numeric(df['나이'], errors='coerce')
        band = (age_num // 10 * 10)
        df['나이대'] = band.astype('Int64').astype(str) + '대'
        df.loc[age_num.isna(), '나이대'] = '(미상)'
    if '시청일' in df.columns:
        df['월'] = pd.to_datetime(df['시청일'], errors='coerce').dt.strftime('%Y-%m')
    return df


def _filtered_audience(db_audience, profile_df, conditions):
    if not conditions or profile_df is None or profile_df.empty:
        return db_audience
    matched = _filter_by_conditions(profile_df.copy(), conditions, db_audience)
    matched_ids = set(matched['R고객번호'].astype(str).unique().tolist())
    return db_audience[db_audience['R고객번호'].astype(str).isin(matched_ids)]


def _resolve_field_column(field, df_columns):
    col = _FIELD_COLUMN_MAP.get(field, field)
    return col if field in _ALLOWED_ROW_COL_FIELDS and col in df_columns else None


def run_pivot_analysis(db_audience, profile_df, spec):
    """spec: {"행": str, "열": str, "측정값": str, "집계방식": str, "차트유형": str, "필터조건": dict}
    반환: pivot_result dict 또는 (조건이 부실하면) None."""
    row_field = (spec.get('행') or '').strip()
    value_key = (spec.get('측정값') or '').strip()
    value_spec = _VALUE_SPECS.get(value_key)
    if not row_field or value_spec is None:
        return None

    df = _filtered_audience(db_audience, profile_df, spec.get('필터조건'))
    if df is None or df.empty:
        return {'행': row_field, '열': spec.get('열') or '', '측정값': value_key, '전체행수': 0, '결과': []}

    df = _add_derived_columns(df)

    row_col = _resolve_field_column(row_field, df.columns)
    col_field = (spec.get('열') or '').strip()
    col_col = _resolve_field_column(col_field, df.columns) if col_field else None
    value_col = value_spec['col']
    if row_col is None or value_col not in df.columns:
        return None

    agg_label = (spec.get('집계방식') or '').strip()
    agg_func = value_spec['aggs'].get(agg_label) or value_spec['aggs'][value_spec['default_agg']]

    safe_df = df.copy()
    pivot_cols = [row_col] + ([col_col] if col_col else [])
    for c in pivot_cols:
        safe_df[c] = safe_df[c].fillna("(미상)").replace("", "(미상)")

    pivot = pd.pivot_table(
        safe_df, index=row_col, columns=col_col if col_col else None,
        values=value_col, aggfunc=agg_func, fill_value=0,
    )
    flat = pivot.reset_index()
    flat.columns = [str(c) for c in flat.columns]
    row_col_name = str(row_col)
    series_cols = [c for c in flat.columns if c != row_col_name]

    divide = value_spec['divide']
    if divide != 1:
        flat[series_cols] = (flat[series_cols] / divide).round(1)
    else:
        flat[series_cols] = flat[series_cols].round(1)

    # 시청일(YYYY-MM-DD)/월(YYYY-MM)은 문자열 정렬이 곧 시간순 정렬이라 별도 처리가 필요 없다.
    flat = flat.sort_values(row_col_name, kind='mergesort').reset_index(drop=True)

    return {
        '행': row_field, '열': col_field, '측정값': value_key,
        '단위': value_spec['unit'], '차트유형': '선' if spec.get('차트유형') == '선' else '막대',
        '행컬럼': row_col_name, '계열컬럼': series_cols,
        '전체행수': len(flat), '결과': flat.to_dict('records'),
    }


def _format_pivot_fallback_reply(pivot_result):
    if pivot_result is None:
        return "죄송해요, 그 요청은 정확히 어떤 기준으로 나눠서 뭘 보여드려야 할지 판단하기 어려웠어요. 예를 들어 '채널별 누적 시청시간 보여줘'처럼 다시 말씀해주시겠어요?"
    rows = pivot_result.get('결과') or []
    if not rows:
        return "말씀하신 조건에 맞는 시청 데이터를 찾지 못했어요. 조건을 조금 다르게 말씀해주시겠어요?"
    return f"{pivot_result['행']}별 {pivot_result['측정값']}({pivot_result['단위']}) 기준으로 총 {pivot_result['전체행수']}개 항목이 나왔어요. 표와 그래프를 확인해보세요."


def process_analysis_turn(messages, user_text, profile_df, db_audience=None):
    """반환: (new_messages, chart_spec)
    chart_spec: {"type": "pivot", "data": pivot_result} 또는 결과가 없으면 None."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df)

    ai_raw = generate_analysis_chat_reply(history_with_user, profile_context_str)
    reply_text, spec = parse_target_conditions(ai_raw)
    spec = spec or {}

    pivot_result = run_pivot_analysis(db_audience, profile_df, spec)
    chart_spec = None
    fallback_reply = _format_pivot_fallback_reply(pivot_result)

    if pivot_result and pivot_result.get('결과'):
        chart_spec = {"type": "pivot", "data": pivot_result}
        ai_reply = generate_segment_insight_reply(
            user_text, str(spec), str(pivot_result['결과'][:10]),
        )
        reply_text = fallback_reply if is_api_error(ai_reply) else ai_reply
    elif spec.get('행') or spec.get('측정값'):
        # AI가 분류는 했지만(행/측정값 중 하나라도 채움) 결과가 비었거나 필드가 무효한 경우
        reply_text = fallback_reply

    new_message = {"role": "assistant", "text": reply_text}
    if chart_spec:
        new_message["chart"] = chart_spec
    new_messages = history_with_user + [new_message]
    return new_messages, chart_spec
