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
import re
import numpy as np
import pandas as pd
from ai_engine.gemini_api import (
    generate_analysis_chat_reply, generate_pivot_insight_reply, generate_comparison_feedback_reply, is_api_error,
)
from database.db_manager import summarize_profile_context, _filter_by_conditions
from utils.response_parser import parse_target_conditions

# 행/열로 쓸 수 있는 필드 -> 실제 컬럼명 (다르면 매핑, 같으면 자기 자신).
# 분석대상별 데이터에 실제로 있는 컬럼만 쓰이므로(예: 콘텐츠 기준엔 성별 없음) 목록은 하나로 둔다.
_FIELD_COLUMN_MAP = {'SO': '시청자SO'}
_ALLOWED_ROW_COL_FIELDS = {
    '성별', '나이대', 'SO', '채널명', '메뉴명', '장르', '시리즈명', '영상명', '시청시작시', '월', '시청일',
    '업로더 구분', '업로더SO', '제작자', '가격유형', '등록월',
    '__전체',  # 내부용: 전체를 한 묶음으로 집계(비교 피드백의 전체값) - AI에게는 노출하지 않음
}
# 🌟 [다중 행/열/값] 표와 그래프가 읽을 수 없을 만큼 커지지 않도록 개수 상한을 둔다
_MAX_ROWS, _MAX_COLS, _MAX_VALUES = 2, 2, 3
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
    '시청': '시청이력 기준 (당사 직원 제외 · {기간})',
    '콘텐츠': '콘텐츠 통계 누적값 기준 (전체 기간 · 직원 시청 포함 · 삭제 콘텐츠 제외)',
}
# 🌟 [목적별 그래프] AI가 분석 목적에 맞게 고른다(프롬프트 참고). 조건이 안 맞으면 막대로 되돌린다.
CHART_TYPES = ('막대', '가로막대', '선', '누적막대', '히트맵', '증감')


def _period_bounds(period):
    """대화에서 정한 기간 {"시작": "2026-07-01" 또는 "2026-07", "종료": ...} -> ('2026-07-01', '2026-08-31').
    월만 오면 시작은 1일, 종료는 말일로. 해석할 수 없는 값은 None(그쪽 제한 없음)."""
    if not isinstance(period, dict):
        return None, None

    def _parse(val, is_end):
        text = str(val or '').strip()
        if not text:
            return None
        if re.fullmatch(r'\d{4}-\d{1,2}', text):
            month = pd.Period(text, freq='M')
            return (month.end_time if is_end else month.start_time).strftime('%Y-%m-%d')
        day = pd.to_datetime(text, errors='coerce')
        return None if pd.isna(day) else day.strftime('%Y-%m-%d')

    return _parse(period.get('시작'), False), _parse(period.get('종료'), True)


def data_period_str(db_audience):
    """AI가 "7월", "최근 3개월" 같은 말을 실제 날짜로 바꿀 수 있게 적재된 기간을 알려준다."""
    if db_audience is None or db_audience.empty or '시청일' not in db_audience.columns:
        return ""
    return (f"- 적재된 시청 데이터 기간: {db_audience['시청일'].min()} ~ {db_audience['시청일'].max()} "
            f"(기간을 정할 때는 이 범위 안에서)")


def _match_label(text, available):
    """증감의 기준/비교 값을 실제 열 값과 맞춘다. "2026-07"은 그대로, "7월"처럼 오면 "YYYY-07"을 찾는다."""
    text = str(text or '').strip()
    if text in available:
        return text
    month = re.search(r'(\d{1,2})\s*월', text)
    if month:
        hits = [a for a in available if re.fullmatch(rf'\d{{4}}-0?{int(month.group(1))}', str(a))]
        return hits[0] if len(hits) == 1 else None
    return None


def _add_change_columns(flat, change, col_cols, series, labels, value_keys, value_specs, agg_label):
    """🌟 [증감 비교] 열(1개)의 두 값(예: 2026-07 → 2026-08) 사이 증감을 행마다 계산해 컬럼으로 붙인다.
    건수/합계류는 증감률(%)도 함께, 평균/비율류는 %p 차이만(비율의 증감률은 오해를 부름)."""
    if not isinstance(change, dict) or len(col_cols) != 1:
        return None
    info = {'측정값': {}}
    for m in value_keys:
        by_label = {labels[n]: n for n in series[m]}
        base, comp = _match_label(change.get('기준'), by_label), _match_label(change.get('비교'), by_label)
        if not base or not comp or base == comp:
            continue
        b, c = pd.to_numeric(flat[by_label[base]]), pd.to_numeric(flat[by_label[comp]])
        diff_col, rate_col = f"증감 ({m})", None
        flat[diff_col] = (c - b).round(1)
        if _measure_is_share(value_specs[m], agg_label):
            rate_col = f"증감률% ({m})"
            flat[rate_col] = ((c - b) / b.where(b != 0) * 100).round(1)
        info.update({'기준': base, '비교': comp})
        info['측정값'][m] = {'기준': by_label[base], '비교': by_label[comp], '증감': diff_col, '증감률': rate_col}
    return info if info['측정값'] else None


def _add_derived_columns(df):
    df = df.copy()
    df['__전체'] = '전체'
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


def _as_fields(val, limit):
    """AI가 행/열/측정값을 리스트로도, "채널명, 성별" 같은 문자열로도 줄 수 있어 둘 다 받는다."""
    if isinstance(val, str):
        items = re.split(r'[,+]', val)
    elif isinstance(val, (list, tuple)):
        items = val
    else:
        items = []
    fields = []
    for item in items:
        item = str(item).strip()
        if item and item not in fields:
            fields.append(item)
    return fields[:limit]


def _target_of(spec):
    return '콘텐츠' if (spec.get('분석대상') or '').strip() == '콘텐츠' else '시청'


def _measure_is_share(value_spec, agg_label):
    """합계/건수/고유수처럼 "나눠 가질 수 있는" 값이면 True(구성비로 비교), 평균/비율이면 False(수준 비교)."""
    if 'ratio' in value_spec:
        return False
    agg = value_spec['aggs'].get(agg_label) or value_spec['aggs'][value_spec['default_agg']]
    return agg in ('sum', 'count', 'nunique')


def _measure_frame(safe_df, row_cols, col_cols, key, value_spec, agg_label):
    """측정값 하나를 행(1~2개) x 열(0~2개)로 집계한 표. 컬럼명은 열 값(없으면 측정값 이름).
    빈 칸(그 조합에 데이터가 없음)은 건수/합계류면 0, 평균/비율류면 빈 값으로 둔다 -
    아무도 안 본 "0대 여자"의 유지율을 0%로 적으면 실제로 0%인 것처럼 읽히기 때문."""
    fill = 0 if _measure_is_share(value_spec, agg_label) else None

    def _pivot(values, aggfunc, fill_value=fill):
        return pd.pivot_table(
            safe_df, index=row_cols, columns=col_cols or None, values=values, aggfunc=aggfunc, fill_value=fill_value,
        )

    if 'ratio' in value_spec:
        num_col, den_col = value_spec['ratio']
        num, den = _pivot(num_col, 'sum', 0), _pivot(den_col, 'sum', 0)
        if not col_cols:  # 열이 없으면 컬럼명이 각각 '클릭수'/'노출수'라 나눗셈 정렬이 어긋난다
            num.columns = den.columns = [key]
        frame = num / den.where(den != 0, np.nan) * 100  # 노출이 0이면 비율은 정의되지 않음(빈 값)
        divide = 1
    else:
        agg = value_spec['aggs'].get(agg_label) or value_spec['aggs'][value_spec['default_agg']]
        frame = _pivot(value_spec['col'], agg)
        divide = value_spec['divide']

    if col_cols:
        frame.columns = [' · '.join(str(v) for v in (c if isinstance(c, tuple) else (c,))) for c in frame.columns]
    else:
        frame.columns = [key]
    frame = (frame / divide).round(1)
    if fill == 0:
        frame = frame.fillna(0)
        for c in frame.columns:
            if (frame[c] % 1 == 0).all():  # 건수/인원처럼 정수면 "409.0건" 대신 409건
                frame[c] = frame[c].astype(int)
    return frame


def run_pivot_analysis(db_audience, profile_df, spec, db_content=None):
    """spec: {"분석대상": "시청"|"콘텐츠", "행": [..최대 2], "열": [..최대 2], "측정값": [..최대 3],
              "집계방식": str, "차트유형": str, "필터조건": dict}  (행/열/측정값은 문자열 1개도 허용)
    반환: pivot_result dict 또는 (조건이 부실하면) None.
      결과: 행 기준 컬럼들 + 계열 컬럼들의 레코드 목록
      계열: {측정값: [그 측정값의 계열 컬럼명]}  / 계열라벨: {계열 컬럼명: 범례 라벨(열 값)}"""
    target = _target_of(spec)
    value_specs = _CONTENT_VALUE_SPECS if target == '콘텐츠' else _VIEW_VALUE_SPECS
    row_fields = _as_fields(spec.get('행'), _MAX_ROWS)
    col_fields = [f for f in _as_fields(spec.get('열'), _MAX_COLS) if f not in row_fields]
    value_keys = [k for k in _as_fields(spec.get('측정값'), _MAX_VALUES) if k in value_specs]
    if not row_fields or not value_keys:
        return None
    start, end = _period_bounds(spec.get('기간')) if target == '시청' else (None, None)
    period_text = f"{start or '처음'} ~ {end or '마지막'}" if (start or end) else "전체 기간"
    empty_result = {'분석대상': target, '데이터기준': _BASIS_LABELS[target].format(기간=period_text),
                    '행': row_fields, '열': col_fields, '측정값': value_keys, '기간': [start, end],
                    '전체행수': 0, '결과': []}

    if target == '콘텐츠':
        # 누적 통계는 시청자 단위 필터조건(성별/나이대 등)이나 기간을 적용할 수 없는 데이터라 무시한다
        df = db_content
        if df is None or df.empty:
            return empty_result
        df = df[~_is_deleted(df)]
    else:
        df = _filtered_audience(db_audience, profile_df, spec.get('필터조건'))
        # 🌟 [대화로 기간 설정] 대화에서 정한 기간은 이 집계에만 적용한다('YYYY-MM-DD' 문자열 비교)
        if df is not None and start:
            df = df[df['시청일'] >= start]
        if df is not None and end:
            df = df[df['시청일'] <= end]
        if df is None or df.empty:
            return empty_result
        df = _with_content_attrs(df, db_content)
        if '영상명' in row_fields + col_fields:
            df = df[~_is_deleted(df)]  # 영상별 성과에서는 삭제된 콘텐츠 제외(시청기록 자체는 보존)

    df = _add_derived_columns(df)

    row_cols = [_resolve_field_column(f, df.columns) for f in row_fields]
    col_cols = [_resolve_field_column(f, df.columns) for f in col_fields]
    if None in row_cols or None in col_cols:
        return None

    safe_df = df.copy()
    for c in row_cols + col_cols:
        safe_df[c] = safe_df[c].fillna("(미상)").replace("", "(미상)")

    agg_label = (spec.get('집계방식') or '').strip()
    frames, series, labels, units, share_cols = [], {}, {}, {}, []
    for key in value_keys:
        vs = value_specs[key]
        needed = list(vs['ratio']) if 'ratio' in vs else [vs['col']]
        if any(c not in safe_df.columns for c in needed):
            return None
        for c in needed:
            if c not in ('R고객번호', '콘텐츠ID'):
                safe_df[c] = pd.to_numeric(safe_df[c], errors='coerce')
        frame = _measure_frame(safe_df, row_cols, col_cols, key, vs, agg_label)
        # 측정값이 여러 개이고 열도 있으면 계열 이름이 겹치지 않게 측정값을 붙인다
        names = [f"{c} ({key})" if col_cols and len(value_keys) > 1 else c for c in frame.columns]
        labels.update({n: (c if col_cols else key) for n, c in zip(names, frame.columns)})
        frame.columns = names
        frames.append(frame)
        series[key] = names
        units[key] = vs['unit']
        if _measure_is_share(vs, agg_label):
            share_cols.extend(names)

    flat = pd.concat(frames, axis=1)
    flat[share_cols] = flat[share_cols].fillna(0)  # 평균/비율류의 빈 칸은 빈 값 그대로
    change = _add_change_columns(flat, spec.get('증감'), col_cols, series, labels, value_keys, value_specs, agg_label)
    flat = flat.astype(object).where(flat.notna(), None).reset_index()  # 빈 값은 JSON 저장 가능한 None으로
    flat.columns = [str(c) for c in flat.columns]
    row_col_names = [str(c) for c in row_cols]
    # 시청일(YYYY-MM-DD)/월·등록월(YYYY-MM)/시청시작시(00시~23시)는 문자열 정렬이 곧 시간순이다.
    flat = flat.sort_values(row_col_names, kind='mergesort').reset_index(drop=True)

    chart_type = spec.get('차트유형') if spec.get('차트유형') in CHART_TYPES else '막대'
    if chart_type == '증감' and not change:
        chart_type = '막대'  # 증감을 계산할 수 없는 스펙(열이 없거나 기준/비교 값이 없음)
    return {
        **empty_result,
        '단위': units, '차트유형': chart_type, '증감': change,
        '행컬럼': row_col_names, '계열': series, '계열라벨': labels,
        '전체행수': len(flat), '결과': flat.to_dict('records'),
    }


def normalize_pivot_result(data):
    """예전(행/열/측정값 1개씩) 형식으로 저장된 대화의 결과도 지금 형식으로 읽을 수 있게 맞춘다."""
    if not data or '계열' in data:
        return data
    measure = data.get('측정값', '')
    series_cols = data.get('계열컬럼') or []
    return {
        **data,
        '행': [data['행']] if isinstance(data.get('행'), str) else data.get('행', []),
        '열': [data['열']] if data.get('열') else [],
        '측정값': [measure], '단위': {measure: data.get('단위', '')},
        '행컬럼': [data['행컬럼']] if data.get('행컬럼') else [],
        '계열': {measure: series_cols}, '계열라벨': {c: c for c in series_cols},
    }


def describe_pivot(data):
    """'시청시작시·나이대별 시청건수, 시청자수' 같은 한 줄 요약."""
    data = normalize_pivot_result(data)
    text = f"{'·'.join(data['행'])}별 {', '.join(data['측정값'])}"
    return text + (f" ({'·'.join(data['열'])} 비교)" if data.get('열') else "")


def _format_pivot_fallback_reply(pivot_result):
    if pivot_result is None:
        return "죄송해요, 그 요청은 정확히 어떤 기준으로 나눠서 뭘 보여드려야 할지 판단하기 어려웠어요. 예를 들어 '채널별 누적 시청시간 보여줘'처럼 다시 말씀해주시겠어요?"
    rows = pivot_result.get('결과') or []
    if not rows:
        return "말씀하신 조건에 맞는 시청 데이터를 찾지 못했어요. 조건을 조금 다르게 말씀해주시겠어요?"
    return (f"{describe_pivot(pivot_result)} 기준으로 총 {pivot_result['전체행수']}개 "
            f"항목이 나왔어요({pivot_result['데이터기준']}). 그래프가 필요하면 답변 아래 버튼을 눌러주세요.")


def insight_rows_str(pivot_result, limit=30):
    """인사이트 문장 생성용 결과 행. 기준값 순서로 앞 10행만 넘기면 AI가 그 안에서만 최댓값을
    찾아 틀린 말을 한다(예: 00~09시만 보고 "09시가 최고"). 행이 많으면 첫 계열 값 기준 상위만 넘긴다."""
    rows = pivot_result['결과']
    if len(rows) > limit:
        first = next(iter(normalize_pivot_result(pivot_result)['계열'].values()))[0]
        rows = sorted(rows, key=lambda r: r[first] if r[first] is not None else float('-inf'), reverse=True)[:limit]
        return f"(전체 {pivot_result['전체행수']}개 중 {first} 상위 {limit}개) {rows}"
    return str(rows)


_COMPARISON_TOP_N = 10


def build_comparison(db_audience, profile_df, spec, db_content=None):
    """🌟 [전체 비교 피드백] 대화로 도출한 결과를 같은 기준의 전체와 비교한 수치를 계산한다.
    AI는 이 수치만 보고 미시/거시 피드백을 쓴다(숫자를 만들지 않음). 비교는 행 기준으로만 한다
    (열까지 교차하면 비교 항목이 폭발적으로 늘어 해석이 흐려진다).
      - 필터조건으로 좁힌 경우: 세그먼트 vs 같은 행 기준의 전체
          합계/건수류는 구성비(%)와 지수(세그먼트 구성비 ÷ 전체 구성비), 평균/비율류는 값 차이
      - 조건 없이 본 경우: 항목별 구성비, 평균/비율류는 전체 평균 대비 차이"""
    target = _target_of(spec)
    value_specs = _CONTENT_VALUE_SPECS if target == '콘텐츠' else _VIEW_VALUE_SPECS
    agg_label = (spec.get('집계방식') or '').strip()
    base = {**spec, '열': [], '증감': None}  # 기간은 그대로 둬서 전체도 같은 기간으로 비교한다
    seg = run_pivot_analysis(db_audience, profile_df, base, db_content)
    if not seg or not seg.get('결과'):
        return None
    has_filter = target == '시청' and bool(spec.get('필터조건'))
    whole_spec = {**base, '필터조건': {}}

    def _overall(s, measures):
        r = run_pivot_analysis(db_audience, profile_df, {**s, '행': ['__전체'], '측정값': measures}, db_content)
        return r['결과'][0] if r and r.get('결과') else {}

    total = run_pivot_analysis(db_audience, profile_df, whole_spec, db_content) if has_filter else None
    seg_all = _overall(base, seg['측정값'])
    tot_all = _overall(whole_spec, seg['측정값']) if has_filter else {}

    def _label(r):
        return ' · '.join(str(r[c]) for c in seg['행컬럼'])

    total_rows = {_label(r): r for r in total['결과']} if total else {}
    items = []
    for m in seg['측정값']:
        is_share = _measure_is_share(value_specs[m], agg_label)
        seg_sum = sum(r[m] for r in seg['결과'])
        tot_sum = sum(r[m] for r in total['결과']) if total else 0
        measure_items = []
        for r in seg['결과']:
            item = {'항목': _label(r), '측정값': m, '값': r[m]}
            if is_share:
                item['이 결과 내 구성비(%)'] = round(r[m] / seg_sum * 100, 1) if seg_sum else 0
                if total:
                    t_share = round(total_rows.get(item['항목'], {}).get(m, 0) / tot_sum * 100, 1) if tot_sum else 0
                    item['전체 구성비(%)'] = t_share
                    item['지수(전체=1.0)'] = round(item['이 결과 내 구성비(%)'] / t_share, 2) if t_share else None
                gap = abs((item.get('지수(전체=1.0)') or 1) - 1) if total else item['이 결과 내 구성비(%)']
            else:
                ref = total_rows.get(item['항목'], {}).get(m) if total else seg_all.get(m)
                item['비교값(' + ('전체 같은 항목' if total else '전체 평균') + ')'] = ref
                item['차이'] = round(r[m] - ref, 1) if ref is not None and r[m] is not None else None
                gap = abs(item['차이'] or 0)
            measure_items.append((gap, item))
        if len(measure_items) > _COMPARISON_TOP_N:
            # 항목이 많으면 규모가 큰 항목과 전체와 차이가 큰 항목만 남긴다
            by_size = sorted(measure_items, key=lambda x: x[1]['값'] or 0, reverse=True)[:_COMPARISON_TOP_N]
            by_gap = sorted(measure_items, key=lambda x: x[0], reverse=True)[:_COMPARISON_TOP_N]
            keep = {id(i) for _, i in by_size + by_gap}
            measure_items = [x for x in measure_items if id(x[1]) in keep]
        items.extend(i for _, i in measure_items)

    scale = {}
    if has_filter:
        seg_scale = _overall(base, ['시청자수', '시청건수'])
        tot_scale = _overall(whole_spec, ['시청자수', '시청건수'])
        for k in ('시청자수', '시청건수'):
            if tot_scale.get(k):
                scale[k] = {'세그먼트': seg_scale.get(k), '전체': tot_scale[k],
                            '비중(%)': round(seg_scale.get(k, 0) / tot_scale[k] * 100, 1)}
    return {
        '분석': describe_pivot(seg), '데이터기준': seg['데이터기준'], '단위': seg['단위'],
        '비교방식': ('조건으로 좁힌 세그먼트 vs 같은 기준의 전체' if has_filter
                  else '조건 없이 전체를 본 결과라 항목별 구성비와 전체 평균 대비 차이로 비교'),
        '세그먼트조건': spec.get('필터조건') or '없음',
        '측정값별 전체값': {m: {'세그먼트': seg_all.get(m), **({'전체': tot_all.get(m)} if has_filter else {})}
                        for m in seg['측정값']},
        '세그먼트 규모': scale, '항목 수': len(seg['결과']), '항목비교': items,
    }


def insight_spec_str(spec, pivot_result):
    """인사이트 문장 생성용 AI 입력 - 데이터 기준과 단위도 함께 넘겨 누적/직원 포함 여부와 단위를 설명하게 한다."""
    return str({**spec, '데이터기준': pivot_result['데이터기준'], '단위': pivot_result['단위']})


_RECENT_TURNS_FOR_INSIGHT = 6


def chart_spec_from(data, chart_type=None):
    """집계 결과(data)로 화면용 chart_spec을 만든다. chart_type이 오면 그래프 종류만 바꾼다."""
    if chart_type in CHART_TYPES and not (chart_type == '증감' and not data.get('증감')):
        data = {**data, '차트유형': chart_type}
    return {"type": "pivot", "data": data}


def _spec_core(spec):
    """같은 집계인지 비교용 - 차트 종류/그래프 요청 여부는 집계 결과에 영향이 없으므로 뺀다."""
    return (
        _target_of(spec), tuple(_as_fields(spec.get('행'), _MAX_ROWS)), tuple(_as_fields(spec.get('열'), _MAX_COLS)),
        tuple(_as_fields(spec.get('측정값'), _MAX_VALUES)), (spec.get('집계방식') or '').strip(),
        str(spec.get('필터조건') or {}), _period_bounds(spec.get('기간')),
        str({k: v for k, v in (spec.get('증감') or {}).items() if v}),
    )


def _history_for_ai(messages):
    """AI가 "그럼 여성만"/"그래프로 만들어줘" 같은 후속 요청을 이어갈 수 있도록, 데이터를 집계한
    답변 뒤에 실제로 쓴 스펙을 붙여서 넘긴다(화면에 보이는 텍스트만으로는 기준을 알 수 없음)."""
    return [
        {**m, 'text': f"{m['text']}\n[이 답변에서 집계한 스펙: {m['spec']}]"} if m.get('spec') else m
        for m in messages
    ]


def _last_data_message(messages):
    return next((m for m in reversed(messages) if m.get('role') == 'assistant' and m.get('data')), None)


_FEEDBACK_REQUEST_TEXT = "🔍 이 결과를 전체와 비교해서 피드백해줘"


def _feedback_message(spec, question, context_messages, profile_df, db_audience, db_content):
    """🌟 [전체 비교 피드백] 비교 수치를 계산하고 AI가 미시/거시 관점 피드백을 쓴다."""
    comparison = build_comparison(db_audience, profile_df, spec, db_content)
    if comparison is None:
        return {"role": "assistant", "text": "비교할 분석 결과를 찾지 못했어요. 먼저 궁금한 걸 물어봐주시면 데이터로 확인해드릴게요."}
    reply = generate_comparison_feedback_reply(
        question, str(comparison), conversation=_history_for_ai(context_messages[-_RECENT_TURNS_FOR_INSIGHT:]),
    )
    if is_api_error(reply):
        reply = "전체와 비교한 수치는 계산했지만 피드백 문장을 만들지 못했어요. 잠시 후 다시 요청해주세요."
    # 이 메시지에는 data를 두지 않는다 - "그래프로 보여줘"는 직전 분석 결과를 그대로 쓰게 하기 위함
    return {"role": "assistant", "text": reply, "spec": spec, "feedback": True}


def process_feedback_request(messages, index, profile_df, db_audience=None, db_content=None):
    """답변 아래 '🔍 전체와 비교 피드백' 버튼용. 반환: new_messages"""
    spec = messages[index].get('spec')
    history = messages + [{"role": "user", "text": _FEEDBACK_REQUEST_TEXT}]
    if not spec:
        return history + [{"role": "assistant", "text": "이 답변에는 비교할 집계 결과가 없어요."}]
    return history + [_feedback_message(spec, _FEEDBACK_REQUEST_TEXT, messages, profile_df, db_audience, db_content)]


def process_analysis_turn(messages, user_text, profile_df, db_audience=None, db_content=None):
    """🌟 [대화형 분석] 한 턴 처리. 반환: (new_messages, chart_spec 또는 None)
    - 데이터가 필요한 질문: 실제로 집계하고, 그 숫자로 대화형 답변을 만든다. 결과는 답변 메시지의
      "data"(+ 쓴 스펙 "spec")에 담고, 그래프는 실무자가 원할 때만 "chart"로 붙인다.
    - 되묻기/상의: AI 답변만 남긴다.
    - "그래프로 만들어줘": 직전과 같은 집계면 다시 계산하지 않고 직전 결과로 그래프를 붙인다.
    - "전체랑 비교해서 피드백해줘": 직전(또는 새로 지정한) 집계를 전체와 비교한 미시/거시 피드백."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = f"{summarize_profile_context(profile_df)}\n{data_period_str(db_audience)}"

    ai_raw = generate_analysis_chat_reply(_history_for_ai(history_with_user), profile_context_str)
    reply_text, spec = parse_target_conditions(ai_raw)
    spec = spec or {}
    wants_chart = spec.pop('그래프요청', False) is True
    wants_feedback = spec.pop('피드백요청', False) is True
    has_spec = bool(_as_fields(spec.get('행'), _MAX_ROWS) and _as_fields(spec.get('측정값'), _MAX_VALUES))
    last = _last_data_message(messages)
    new_message = {"role": "assistant", "text": reply_text}

    if wants_feedback:
        target_spec = spec if has_spec else (last.get('spec') if last else None)
        if target_spec:
            new_message = _feedback_message(target_spec, user_text, messages, profile_df, db_audience, db_content)
        else:
            new_message['text'] = "아직 비교할 분석 결과가 없어요. 먼저 궁금한 걸 물어봐주시면 데이터로 확인해드릴게요."
    elif wants_chart and last and (not has_spec or _spec_core(spec) == _spec_core(last.get('spec') or {})):
        new_message.update(
            text=f"방금 본 결과({describe_pivot(last['data'])})를 그래프로 만들었어요.",
            data=last['data'], spec=last.get('spec'),
            chart=chart_spec_from(last['data'], spec.get('차트유형')),
        )
    elif has_spec:
        pivot_result = run_pivot_analysis(db_audience, profile_df, spec, db_content)
        if pivot_result and pivot_result.get('결과'):
            ai_reply = generate_pivot_insight_reply(
                user_text, insight_spec_str(spec, pivot_result), insight_rows_str(pivot_result),
                conversation=_history_for_ai(messages[-_RECENT_TURNS_FOR_INSIGHT:]), follow_up=True,
            )
            new_message.update(
                text=_format_pivot_fallback_reply(pivot_result) if is_api_error(ai_reply) else ai_reply,
                data=pivot_result, spec=spec,
            )
            if wants_chart:
                new_message['chart'] = chart_spec_from(pivot_result)
        else:
            # AI가 스펙은 정했지만 결과가 비었거나 필드가 무효한 경우
            new_message['text'] = _format_pivot_fallback_reply(pivot_result)
    elif wants_chart:
        new_message['text'] = "아직 그래프로 만들 분석 결과가 없어요. 먼저 궁금한 걸 물어봐주시면 데이터로 확인해드릴게요."

    if not new_message['text']:
        new_message['text'] = "어떤 걸 살펴볼까요? 궁금한 점을 편하게 말씀해주세요."
    return history_with_user + [new_message], new_message.get('chart')


if __name__ == "__main__":
    import json
    content = pd.DataFrame({
        '콘텐츠ID': ['a', 'b', 'c'], '채널명': ['X', 'X', 'Y'], '영상명': ['A', 'B', 'C'],
        '노출수': [10000, 1, 50], '클릭수': [100, 1, 5], '조회수': [7, 3, 9],
        '업로더 구분': ['당사직원', '이웃파트너', '당사직원'], '등록일': ['2026-01-05', '2026-02-01', '2026-02-09'],
        '삭제 여부': ['X', 'X', 'O'],
    })
    views = pd.DataFrame({
        'R고객번호': ['1', '1', '2', '3', '4'], '콘텐츠ID': ['a', 'a', 'b', 'c', 'b'], '영상명': ['A', 'A', 'B', 'C', 'B'],
        '채널명': ['X', 'X', 'X', 'Y', 'X'], '시청 유지율': [100.0, 50.0, 99.95, 10.0, np.nan], '시청시간': [60, 30, 60, 6, 99],
        '시청일': ['2026-08-01'] * 5, '성별': ['여자', '여자', '남자', '여자', '남자'],
    })
    profile = views[['R고객번호', '성별']].drop_duplicates()

    def one(spec, series=None):
        r = run_pivot_analysis(views, profile, spec, content)
        first = next(iter(r['계열'].values()))[0]
        return {row[r['행컬럼'][0]]: row[series or first] for row in r['결과']}

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

    # 다중 행/열/값: 행 2개(문자열로 와도 인식) x 열 1개 x 측정값 2개
    multi = run_pivot_analysis(views, profile, {'행': '채널명, 영상명', '열': ['성별'], '측정값': ['시청건수', '시청자수']}, content)
    assert multi['행컬럼'] == ['채널명', '영상명'] and set(multi['계열']) == {'시청건수', '시청자수'}
    row_a = next(r for r in multi['결과'] if r['영상명'] == 'A')
    assert row_a['여자 (시청건수)'] == 2 and row_a['남자 (시청건수)'] == 0 and row_a['여자 (시청자수)'] == 1
    assert multi['계열라벨']['여자 (시청건수)'] == '여자' and multi['단위'] == {'시청건수': '건', '시청자수': '명'}
    # 데이터가 없는 조합: 건수는 0, 유지율은 빈 값(None) - 0%로 오해되지 않게
    gaps = run_pivot_analysis(views, profile, {'행': '영상명', '열': '성별', '측정값': ['시청건수', '시청유지율']}, content)
    row_a = next(r for r in gaps['결과'] if r['영상명'] == 'A')
    assert row_a['남자 (시청건수)'] == 0 and row_a['남자 (시청유지율)'] is None and row_a['여자 (시청유지율)'] == 75.0
    json.dumps(gaps, ensure_ascii=False)  # 대화 저장(JSON)이 가능해야 한다
    assert describe_pivot(multi) == "채널명·영상명별 시청건수, 시청자수 (성별 비교)"
    old = {'행': '채널명', '열': '', '측정값': '시청건수', '단위': '건', '행컬럼': '채널명', '계열컬럼': ['시청건수']}
    assert normalize_pivot_result(old)['계열'] == {'시청건수': ['시청건수']}, "예전 형식 대화도 읽힌다"

    # 기간 + 증감: SO별 7월 대비 8월 시청자수(MAU)
    mau = pd.DataFrame({
        'R고객번호': ['1', '2', '3', '1', '4', '5', '6'], '콘텐츠ID': ['a'] * 7, '시청자SO': ['P', 'P', 'Q', 'P', 'P', 'Q', 'R'],
        '시청 유지율': [10.0] * 7, '시청일': ['2026-06-30', '2026-07-05', '2026-07-09', '2026-08-01', '2026-08-02', '2026-08-31', '2026-09-01'],
    })
    ch = run_pivot_analysis(mau, None, {
        '행': 'SO', '열': '월', '측정값': ['시청자수', '시청유지율'], '기간': {'시작': '2026-07', '종료': '2026-08'},
        '증감': {'기준': '7월', '비교': '2026-08'}, '차트유형': '증감'}, None)
    by_so = {r['시청자SO']: r for r in ch['결과']}
    assert ch['기간'] == ['2026-07-01', '2026-08-31'] and '2026-07-01 ~ 2026-08-31' in ch['데이터기준']
    assert by_so['P']['증감 (시청자수)'] == 1 and by_so['P']['증감률% (시청자수)'] == 100.0, "P: 7월 1명(2번) → 8월 2명(1,4번)"
    assert by_so['Q']['증감 (시청자수)'] == 0 and 'R' not in by_so, "6/30·9/1 기록은 기간 밖"
    assert ch['증감']['측정값']['시청유지율']['증감률'] is None, "비율류는 증감률 없이 %p 차이만"
    assert ch['차트유형'] == '증감' and _spec_core({'기간': {'시작': '2026-07'}}) != _spec_core({})
    no_change = run_pivot_analysis(mau, None, {'행': 'SO', '측정값': '시청자수', '차트유형': '증감'}, None)
    assert no_change['차트유형'] == '막대' and no_change['증감'] is None, "증감 불가면 막대로"

    # 전체 비교: 여성(1,3번 고객) vs 전체
    cmp_ = build_comparison(views, profile, {'행': '채널명', '측정값': ['시청건수', '시청유지율'], '필터조건': {'성별': '여자'}}, content)
    by = {(i['항목'], i['측정값']): i for i in cmp_['항목비교']}
    assert by[('X', '시청건수')]['이 결과 내 구성비(%)'] == 66.7 and by[('X', '시청건수')]['전체 구성비(%)'] == 80.0
    assert by[('Y', '시청건수')]['지수(전체=1.0)'] > 1 > by[('X', '시청건수')]['지수(전체=1.0)']
    assert by[('X', '시청유지율')]['차이'] == -8.3 and by[('Y', '시청유지율')]['차이'] == 0
    assert cmp_['세그먼트 규모']['시청자수'] == {'세그먼트': 2, '전체': 4, '비중(%)': 50.0}
    no_filter = build_comparison(views, profile, {'행': '채널명', '측정값': '시청유지율'}, content)
    assert no_filter['항목비교'][0]['비교값(전체 평균)'] == 65.0 and not no_filter['세그먼트 규모']

    # 대화형 턴: AI 호출은 가짜로 바꿔서 흐름만 검사한다
    import json
    ai_calls = []

    def fake_turn(spec, text="확인해볼게요"):
        globals()['generate_analysis_chat_reply'] = lambda *a: f"{text}\n```json\n{json.dumps(spec, ensure_ascii=False)}\n```"
        globals()['generate_pivot_insight_reply'] = lambda *a, **k: ai_calls.append(k) or "A가 가장 많아요. 채널별로도 볼까요?"
        globals()['summarize_profile_context'] = lambda *a: ""
        globals()['generate_comparison_feedback_reply'] = lambda *a, **k: "🔬 미시적 관점 ... 🌐 거시적 관점 ..."

    msgs = []
    fake_turn({'행': '영상명', '측정값': '시청건수', '그래프요청': False})
    msgs, chart = process_analysis_turn(msgs, "영상별로 얼마나 봤어?", None, views, content)
    assert chart is None and msgs[-1]['data']['결과'] and 'chart' not in msgs[-1], "숫자로만 답하고 그래프는 안 만든다"
    assert ai_calls[-1]['follow_up'] is True
    n_calls = len(ai_calls)
    fake_turn({'행': '영상명', '측정값': '시청건수', '차트유형': '선', '그래프요청': True})
    msgs, chart = process_analysis_turn(msgs, "그래프로 보여줘", None, views, content)
    assert chart and chart['data']['차트유형'] == '선' and len(ai_calls) == n_calls, "같은 집계면 재계산/재설명 없이 그래프만"
    fake_turn({'행': '', '측정값': '', '그래프요청': False}, text="시청자수 기준으로 볼까요, 누적 조회수 기준으로 볼까요?")
    msgs, chart = process_analysis_turn(msgs, "인기 콘텐츠 알려줘", None, views, content)
    assert chart is None and 'data' not in msgs[-1] and msgs[-1]['text'].startswith("시청자수 기준")
    assert "[이 답변에서 집계한 스펙" in _history_for_ai(msgs)[1]['text']
    # 피드백: 말로 요청하면 직전 집계 스펙으로, 버튼이면 그 답변의 스펙으로 비교한다
    fake_turn({'행': [], '측정값': [], '피드백요청': True})
    msgs, _ = process_analysis_turn(msgs, "전체랑 비교해서 피드백 줘", profile, views, content)
    assert msgs[-1].get('feedback') and msgs[-1]['spec']['행'] == '영상명' and 'data' not in msgs[-1]
    msgs = process_feedback_request(msgs, 1, profile, views, content)
    assert msgs[-2]['text'] == _FEEDBACK_REQUEST_TEXT and msgs[-1].get('feedback')
    print("analysis_service self-check OK")
