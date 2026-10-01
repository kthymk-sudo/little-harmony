# services/analysis_service.py
# ============================================================
# 분석 탭과 보고서·타겟팅의 간단한 수치 확인이 함께 쓰는 데이터 가공 (Streamlit 비의존 - 단위 테스트 가능).
# 분석은 AI 코드 실행형(services/code_analyst.py)이고, 여기에는 그 데이터를 준비하는 파생 컬럼
# (나이대/월/SO권역/등록월 등), 콘텐츠 정보 붙이기, 그래프용 결과 형식(pivot_result)만 남아 있다.
# ============================================================
import pandas as pd
from config import SO_TO_REGION, REGION_ORDER as _REGION_ORDER

_REGION_COLUMNS = {'SO권역', '업로더권역'}
_CONTENT_ATTR_COLS = ['업로더 구분', '업로더SO', '제작자', '가격유형', '등록일', '삭제 여부']
_COMPLETION_THRESHOLD = 99.9  # 유지율이 이 값 이상이면 "끝까지 본 시청"(구 시스템 기준 그대로)
# 🌟 [목적별 그래프] AI가 분석 목적에 맞게 고른다(프롬프트 참고). 조건이 안 맞으면 막대로 되돌린다.
CHART_TYPES = ('막대', '가로막대', '선', '누적막대', '히트맵', '증감')


def _region_sort_key(series):
    """권역은 가나다순이 아니라 정해둔 순서(대전→충청→…→대구)로, 목록에 없는 값은 그 뒤에."""
    if series.name not in _REGION_COLUMNS:
        return series
    return series.map(lambda v: (_REGION_ORDER.index(v), '') if v in _REGION_ORDER else (len(_REGION_ORDER), str(v)))


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
    for raw, region in (('시청자SO', 'SO권역'), ('업로더SO', '업로더권역')):
        if raw in df.columns:
            df[region] = df[raw].map(lambda v: SO_TO_REGION.get(v, v))
    return df


def _with_content_attrs(df, db_content):
    """시청이력에 콘텐츠 통계의 업로더/제작자/등록일 등을 콘텐츠ID로 붙인다(기준 필드 용도)."""
    if db_content is None or db_content.empty or '콘텐츠ID' not in db_content.columns:
        return df
    attrs = [c for c in _CONTENT_ATTR_COLS if c in db_content.columns and c not in df.columns]
    if not attrs:
        return df
    lookup = db_content[['콘텐츠ID'] + attrs].drop_duplicates(subset=['콘텐츠ID'])
    return df.merge(lookup, on='콘텐츠ID', how='left')


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


def chart_spec_from(data, chart_type=None):
    """집계 결과(data)로 화면용 chart_spec을 만든다. chart_type이 오면 그래프 종류만 바꾼다."""
    if chart_type in CHART_TYPES and not (chart_type == '증감' and not data.get('증감')):
        data = {**data, '차트유형': chart_type}
    return {"type": "pivot", "data": data}


if __name__ == "__main__":
    views = pd.DataFrame({
        'R고객번호': ['1', '2', '3'], '콘텐츠ID': ['a', 'b', 'a'], '나이': [45, 62, None], '시청일': ['2026-07-01', '2026-08-02', '2026-08-03'],
        '시청 유지율': [100.0, 50.0, None], '시청자SO': ['㈜씨엠비동대전방송', '㈜씨엠비수성방송', '모르는SO'],
    })
    content = pd.DataFrame({'콘텐츠ID': ['a', 'b'], '업로더 구분': ['당사직원', '이웃파트너'], '등록일': ['2026-01-05', '2026-02-01']})
    # 파생 컬럼: 나이대(미상 포함)·월·SO권역(목록에 없는 SO는 그대로)·완료율(유지율을 모르면 빈 값)
    d = _add_derived_columns(_with_content_attrs(views, content))
    assert list(d['나이대']) == ['40대', '60대', '(미상)'] and list(d['월']) == ['2026-07', '2026-08', '2026-08']
    assert list(d['SO권역']) == ['대전', '대구', '모르는SO'] and list(d['업로더 구분']) == ['당사직원', '이웃파트너', '당사직원']
    assert list(d['__완료'][:2]) == [100.0, 0.0] and pd.isna(d['__완료'][2]) and views.shape[1] == 6, "원본은 그대로"
    assert _with_content_attrs(views, None) is views
    # 권역은 정해둔 순서(대전→…→대구), 목록 밖은 뒤로
    s = pd.Series(['대구', '모르는SO', '대전'], name='SO권역')
    assert list(s[sorted(range(3), key=lambda i: _region_sort_key(s).iloc[i])]) == ['대전', '대구', '모르는SO']
    # 그래프 결과 형식: 예전 저장 형식도 읽히고, 증감이 없는 결과는 증감 그래프로 바꾸지 않는다
    old = {'행': '채널명', '열': '', '측정값': '시청건수', '단위': '건', '행컬럼': '채널명', '계열컬럼': ['시청건수']}
    assert normalize_pivot_result(old)['계열'] == {'시청건수': ['시청건수']} and describe_pivot(old) == "채널명별 시청건수"
    new = {'행': ['월', '나이대'], '열': ['성별'], '측정값': ['시청건수', '시청자수'], '계열': {}, '차트유형': '막대', '증감': None}
    assert describe_pivot(new) == "월·나이대별 시청건수, 시청자수 (성별 비교)"
    assert chart_spec_from(new, '선')['data']['차트유형'] == '선' and chart_spec_from(new, '증감')['data']['차트유형'] == '막대'
    print("analysis_service self-check OK")
