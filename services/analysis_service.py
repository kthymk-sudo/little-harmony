# services/analysis_service.py
# ============================================================
# 📊 분석 탭 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
# 고정된 질문 유형(그룹현황/순위) 대신, AI가 번역한 피벗 스펙(행/열/측정값/
# 집계방식/차트유형)을 받아 시청이력 원본(db_audience, 리스트 컬럼이 없는
# 평평한 이벤트 단위 데이터)을 직접 피벗 집계한다 - 구 시스템
# visualization/chart_generator.py의 피벗 로직을 Streamlit 의존 없이 이식.
# AI는 숫자를 전혀 만들지 않는다: 행/열/측정값을 해석만 하고, 실제 집계는
# 전부 이 파일의 결정론적 pandas 연산이 담당한다.
#
# 🌟 [콘텐츠 분석] "분석대상"으로 데이터 기준을 나눈다(기준이 다른 숫자를 한 표에
# 섞지 않기 위함 - 구 시스템은 누적 통계와 기간 시청이력 집계를 한 표에 나란히 뒀다).
#   - 시청(기본): 시청이력 원본. 직원 제외 + 시청기간 적용. 콘텐츠 통계의 업로더/제작자/
#     등록월 등은 콘텐츠ID로 붙여서 "기준"으로만 쓴다. 영상명별로 볼 때는 삭제된 콘텐츠 제외.
#   - 콘텐츠: 콘텐츠 통계 파일의 누적값(전체 기간, 직원 시청 포함). 노출/클릭/찜/댓글처럼
#     시청이력에 없는 지표 전용. 삭제된 콘텐츠 제외.
# ============================================================
import numpy as np
import pandas as pd
from ai_engine.gemini_api import generate_analysis_chat_reply, generate_pivot_insight_reply, is_api_error
from database.db_manager import summarize_profile_context, _filter_by_conditions
from utils.response_parser import parse_target_conditions

# 행/열로 쓸 수 있는 필드 -> 실제 컬럼명 (다르면 매핑, 같으면 자기 자신).
# 분석대상별 데이터에 실제로 있는 컬럼만 쓰이므로(예: 콘텐츠 기준엔 성별 없음) 목록은 하나로 둔다.
_FIELD_COLUMN_MAP = {'SO': '시청자SO'}
_ALLOWED_ROW_COL_FIELDS = {
    '성별', '나이대', 'SO', '채널명', '메뉴명', '장르', '시리즈명', '영상명', '시청시작시', '월', '시청일',
    '업로더 구분', '업로더SO', '제작자', '가격유형', '등록월',
}
_CONTENT_ATTR_COLS = ['업로더 구분', '업로더SO', '제작자', '가격유형', '등록일', '삭제 여부']
_COMPLETION_THRESHOLD = 99.9  # 유지율이 이 값 이상이면 "끝까지 본 시청"(구 시스템 기준 그대로)

_SUM_MEAN = {'합계': 'sum', '평균': 'mean'}

# 측정값 -> {집계에 쓸 컬럼, 허용 집계방식(AI 표현 -> pandas aggfunc), 표시 단위/나누기}
# 'ratio'가 있으면 (분자, 분모) 합계의 비율(%)로 계산한다 - 콘텐츠별 비율을 단순 평균하면
# 노출 1회짜리 콘텐츠의 100%가 노출 1만 회짜리와 같은 무게로 섞이기 때문.
_VIEW_VALUE_SPECS = {
    '시청시간': {'col': '시청시간', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 60, 'unit': '분'},
    '시청유지율': {'col': '시청 유지율', 'aggs': {'평균': 'mean'}, 'default_agg': '평균', 'divide': 1, 'unit': '%'},
    '시청완료율': {'col': '__완료', 'aggs': {'평균': 'mean'}, 'default_agg': '평균', 'divide': 1, 'unit': '%'},
    '시청자수': {'col': 'R고객번호', 'aggs': {'고유값수': 'nunique'}, 'default_agg': '고유값수', 'divide': 1, 'unit': '명'},
    '시청건수': {'col': 'R고객번호', 'aggs': {'개수': 'count'}, 'default_agg': '개수', 'divide': 1, 'unit': '건'},
    '콘텐츠수': {'col': '콘텐츠ID', 'aggs': {'고유값수': 'nunique'}, 'default_agg': '고유값수', 'divide': 1, 'unit': '개'},
}
_CONTENT_VALUE_SPECS = {
    '조회수': {'col': '조회수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '회'},
    '이용자수': {'col': '이용자수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '명'},
    '찜하기수': {'col': '찜하기 수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '회'},
    '댓글수': {'col': '댓글 수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '개'},
    '노출수': {'col': '노출수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '회'},
    '클릭수': {'col': '클릭수', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 1, 'unit': '회'},
    '노출클릭율': {'ratio': ('클릭수', '노출수'), 'unit': '%'},
    '평균시청지속시간': {'col': '평균 시청 지속시간', 'aggs': {'평균': 'mean'}, 'default_agg': '평균', 'divide': 60, 'unit': '분'},
    '누적시청시간': {'col': '누적 시청시간', 'aggs': _SUM_MEAN, 'default_agg': '합계', 'divide': 60, 'unit': '분'},
    '콘텐츠수': {'col': '콘텐츠ID', 'aggs': {'고유값수': 'nunique'}, 'default_agg': '고유값수', 'divide': 1, 'unit': '개'},
}
_BASIS_LABELS = {
    '시청': '시청이력 기준 (당사 직원 제외 · 선택한 시청기간)',
    '콘텐츠': '콘텐츠 통계 누적값 기준 (전체 기간 · 직원 시청 포함 · 삭제 콘텐츠 제외)',
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
    if '등록일' in df.columns:
        df['등록월'] = pd.to_datetime(df['등록일'], errors='coerce').dt.strftime('%Y-%m')
    if '시청 유지율' in df.columns:
        # 유지율을 모르는 시청(러닝타임 0)은 "끝까지 안 봄"이 아니라 계산에서 빠져야 하므로 NaN 유지
        retention = pd.to_numeric(df['시청 유지율'], errors='coerce')
        df['__완료'] = ((retention >= _COMPLETION_THRESHOLD) * 100.0).where(retention.notna())
    return df


def _is_deleted(df):
    return df['삭제 여부'].astype(str).str.strip().str.upper() == 'O' if '삭제 여부' in df.columns else False


def _with_content_attrs(df, db_content):
    """시청이력에 콘텐츠 통계의 업로더/제작자/등록일 등을 콘텐츠ID로 붙인다(기준 필드 용도)."""
    if db_content is None or db_content.empty or '콘텐츠ID' not in db_content.columns:
        return df
    attrs = [c for c in _CONTENT_ATTR_COLS if c in db_content.columns and c not in df.columns]
    if not attrs:
        return df
    lookup = db_content[['콘텐츠ID'] + attrs].drop_duplicates(subset=['콘텐츠ID'])
    return df.merge(lookup, on='콘텐츠ID', how='left')


def _filtered_audience(db_audience, profile_df, conditions):
    if not conditions or profile_df is None or profile_df.empty:
        return db_audience
    matched = _filter_by_conditions(profile_df.copy(), conditions, db_audience)
    matched_ids = set(matched['R고객번호'].astype(str).unique().tolist())
    return db_audience[db_audience['R고객번호'].astype(str).isin(matched_ids)]


def _resolve_field_column(field, df_columns):
    col = _FIELD_COLUMN_MAP.get(field, field)
    return col if field in _ALLOWED_ROW_COL_FIELDS and col in df_columns else None


def run_pivot_analysis(db_audience, profile_df, spec, db_content=None):
    """spec: {"분석대상": "시청"|"콘텐츠", "행": str, "열": str, "측정값": str, "집계방식": str,
              "차트유형": str, "필터조건": dict}
    반환: pivot_result dict 또는 (조건이 부실하면) None."""
    target = '콘텐츠' if (spec.get('분석대상') or '').strip() == '콘텐츠' else '시청'
    row_field = (spec.get('행') or '').strip()
    col_field = (spec.get('열') or '').strip()
    value_key = (spec.get('측정값') or '').strip()
    value_spec = (_CONTENT_VALUE_SPECS if target == '콘텐츠' else _VIEW_VALUE_SPECS).get(value_key)
    if not row_field or value_spec is None:
        return None
    empty_result = {'분석대상': target, '데이터기준': _BASIS_LABELS[target], '행': row_field, '열': col_field,
                    '측정값': value_key, '전체행수': 0, '결과': []}

    if target == '콘텐츠':
        # 누적 통계는 시청자 단위 필터조건(성별/나이대 등)을 적용할 수 없는 데이터라 무시한다
        df = db_content
        if df is None or df.empty:
            return empty_result
        df = df[~_is_deleted(df)]
    else:
        df = _filtered_audience(db_audience, profile_df, spec.get('필터조건'))
        if df is None or df.empty:
            return empty_result
        df = _with_content_attrs(df, db_content)
        if '영상명' in (row_field, col_field):
            df = df[~_is_deleted(df)]  # 영상별 성과에서는 삭제된 콘텐츠 제외(시청기록 자체는 보존)

    df = _add_derived_columns(df)

    row_col = _resolve_field_column(row_field, df.columns)
    col_col = _resolve_field_column(col_field, df.columns) if col_field else None
    value_cols = list(value_spec['ratio']) if 'ratio' in value_spec else [value_spec['col']]
    if row_col is None or any(c not in df.columns for c in value_cols):
        return None

    safe_df = df.copy()
    pivot_cols = [row_col] + ([col_col] if col_col else [])
    for c in pivot_cols:
        safe_df[c] = safe_df[c].fillna("(미상)").replace("", "(미상)")

    def _pivot(values, aggfunc):
        return pd.pivot_table(
            safe_df, index=row_col, columns=col_col if col_col else None,
            values=values, aggfunc=aggfunc, fill_value=0,
        )

    if 'ratio' in value_spec:
        num_col, den_col = value_spec['ratio']
        for c in value_spec['ratio']:
            safe_df[c] = pd.to_numeric(safe_df[c], errors='coerce').fillna(0)
        num, den = _pivot(num_col, 'sum'), _pivot(den_col, 'sum')
        if not col_col:  # 열이 없으면 컬럼명이 각각 '클릭수'/'노출수'라 나눗셈 정렬이 어긋난다
            num.columns = den.columns = [value_key]
        pivot = (num / den.where(den != 0, np.nan) * 100).fillna(0)
        divide = 1
    else:
        agg_label = (spec.get('집계방식') or '').strip()
        agg_func = value_spec['aggs'].get(agg_label) or value_spec['aggs'][value_spec['default_agg']]
        if agg_func in ('sum', 'mean'):
            safe_df[value_cols[0]] = pd.to_numeric(safe_df[value_cols[0]], errors='coerce')
        pivot = _pivot(value_cols[0], agg_func)
        divide = value_spec['divide']

    flat = pivot.reset_index()
    flat.columns = [str(c) for c in flat.columns]
    row_col_name = str(row_col)
    if not col_col:  # 계열이 하나면 내부 컬럼명(R고객번호 등) 대신 측정값 이름으로 보여준다
        flat.columns = [row_col_name, value_key]
    series_cols = [c for c in flat.columns if c != row_col_name]
    flat[series_cols] = (flat[series_cols] / divide).round(1)
    if all((flat[c] % 1 == 0).all() for c in series_cols):  # 건수/인원처럼 정수면 "409.0건" 대신 409건
        flat[series_cols] = flat[series_cols].astype(int)

    # 시청일(YYYY-MM-DD)/월·등록월(YYYY-MM)/시청시작시(00시~23시)는 문자열 정렬이 곧 시간순이다.
    flat = flat.sort_values(row_col_name, kind='mergesort').reset_index(drop=True)

    return {
        **empty_result,
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
    return (f"{pivot_result['행']}별 {pivot_result['측정값']}({pivot_result['단위']}) 기준으로 총 {pivot_result['전체행수']}개 "
            f"항목이 나왔어요({pivot_result['데이터기준']}). 표와 그래프를 확인해보세요.")


def insight_rows_str(pivot_result, limit=30):
    """인사이트 문장 생성용 결과 행. 기준값 순서로 앞 10행만 넘기면 AI가 그 안에서만 최댓값을
    찾아 틀린 말을 한다(예: 00~09시만 보고 "09시가 최고"). 행이 많으면 첫 계열 값 기준 상위만 넘긴다."""
    rows = pivot_result['결과']
    if len(rows) > limit:
        first = pivot_result['계열컬럼'][0]
        rows = sorted(rows, key=lambda r: r[first], reverse=True)[:limit]
        return f"(전체 {pivot_result['전체행수']}개 중 {first} 상위 {limit}개) {rows}"
    return str(rows)


def insight_spec_str(spec, pivot_result):
    """인사이트 문장 생성용 AI 입력 - 데이터 기준과 단위도 함께 넘겨 누적/직원 포함 여부와 단위를 설명하게 한다."""
    return str({**spec, '데이터기준': pivot_result['데이터기준'], '단위': pivot_result['단위']})


def process_analysis_turn(messages, user_text, profile_df, db_audience=None, db_content=None):
    """반환: (new_messages, chart_spec)
    chart_spec: {"type": "pivot", "data": pivot_result} 또는 결과가 없으면 None."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df)

    ai_raw = generate_analysis_chat_reply(history_with_user, profile_context_str)
    reply_text, spec = parse_target_conditions(ai_raw)
    spec = spec or {}

    pivot_result = run_pivot_analysis(db_audience, profile_df, spec, db_content)
    chart_spec = None
    fallback_reply = _format_pivot_fallback_reply(pivot_result)

    if pivot_result and pivot_result.get('결과'):
        chart_spec = {"type": "pivot", "data": pivot_result}
        ai_reply = generate_pivot_insight_reply(
            user_text, insight_spec_str(spec, pivot_result), insight_rows_str(pivot_result),
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


if __name__ == "__main__":
    content = pd.DataFrame({
        '콘텐츠ID': ['a', 'b', 'c'], '채널명': ['X', 'X', 'Y'], '영상명': ['A', 'B', 'C'],
        '노출수': [10000, 1, 50], '클릭수': [100, 1, 5], '조회수': [7, 3, 9],
        '업로더 구분': ['당사직원', '이웃파트너', '당사직원'], '등록일': ['2026-01-05', '2026-02-01', '2026-02-09'],
        '삭제 여부': ['X', 'X', 'O'],
    })
    views = pd.DataFrame({
        'R고객번호': ['1', '1', '2', '3', '4'], '콘텐츠ID': ['a', 'a', 'b', 'c', 'b'], '영상명': ['A', 'A', 'B', 'C', 'B'],
        '채널명': ['X', 'X', 'X', 'Y', 'X'], '시청 유지율': [100.0, 50.0, 99.95, 10.0, np.nan], '시청시간': [60, 30, 60, 6, 99],
        '시청일': ['2026-08-01'] * 5,
    })

    def one(spec, series=None):
        r = run_pivot_analysis(views, None, spec, content)
        return {row[r['행컬럼']]: row[series or r['계열컬럼'][0]] for row in r['결과']}

    # 노출클릭율은 비율의 평균이 아니라 합계의 비율: X = (100+1)/(10000+1), 삭제된 c(Y)는 제외
    assert one({'분석대상': '콘텐츠', '행': '채널명', '측정값': '노출클릭율'}) == {'X': 1.0}
    assert one({'분석대상': '콘텐츠', '행': '등록월', '측정값': '조회수'}) == {'2026-01': 7, '2026-02': 3}
    # 시청 기준: 채널별로는 삭제 콘텐츠 시청도 남고, 영상별로 볼 때만 빠진다
    assert one({'행': '채널명', '측정값': '시청건수'}) == {'X': 4, 'Y': 1}
    assert one({'행': '영상명', '측정값': '시청건수'}) == {'A': 2, 'B': 2}
    # 유지율을 모르는 시청(NaN)은 완료율/유지율 계산에서 빠진다: B는 99.95%짜리 1건만 → 100%
    assert one({'행': '영상명', '측정값': '시청완료율'}) == {'A': 50.0, 'B': 100.0}
    assert one({'행': '영상명', '측정값': '시청유지율'}) == {'A': 75.0, 'B': 100.0}
    assert one({'행': '업로더 구분', '측정값': '시청자수'}) == {'당사직원': 2, '이웃파트너': 2}
    assert run_pivot_analysis(views, None, {'분석대상': '콘텐츠', '행': '채널명', '측정값': '시청건수'}, content) is None
    print("analysis_service self-check OK")
