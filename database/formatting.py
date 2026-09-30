# database/formatting.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "확정된 조건/집계 결과를 사람이 읽는
# 문장으로 바꿔주는" 순수 포맷팅 함수만 분리.
# ============================================================
_CONDITION_LABEL_MAP = {
    '성별': '성별', '나이대': '나이대', '나이최소': '나이(최소)', '나이최대': '나이(최대)',
    'SO': 'SO(지역)',
    '선호장르': '선호장르', '선호시청시간대': '선호시청시간대',
    '선호채널': '선호채널', '선호메뉴': '선호메뉴',
    '최소시청횟수': '최소시청횟수', '최근시청일이후': '최근시청일이후',
    '최소시청유지율': '최소 시청유지율(%)',
    '최소총시청시간_분': '최소 총시청시간(분)',
    '최소시청콘텐츠수': '최소 시청콘텐츠수',
    '활동세그먼트': '활동세그먼트',
    '콘텐츠명포함': '콘텐츠명 포함',
    '채널명포함': '채널명 포함',
}


_WITHIN_LABELS = {'장르포함': '장르', '채널명포함': '채널', '메뉴명포함': '메뉴', '콘텐츠명포함': '콘텐츠', '시리즈명포함': '시리즈'}


def _join(values, max_items):
    """값 목록을 쉼표로 잇는다. max_items를 주면(화면 요약용) 그보다 긴 목록은 앞 몇 개만 보이고 '외 N개'로 줄인다."""
    values = [str(v) for v in (values if isinstance(values, list) else [values])]
    if max_items and len(values) > max_items:
        return ",".join(values[:max_items]) + f" 외 {len(values) - max_items}개"
    return ",".join(values)


def _format_within_part(within, max_items=None, same_genres=None):
    """🌟 [기간 안에 한 행동] {"기간":..., "장르포함": [...]} -> "기간 내 시청(2026-08 · 장르 트로트)"
    same_genres: 화면 요약용 - 장르포함이 이 목록(선호장르)과 같으면 목록을 또 나열하지 않고 '선호장르와 동일'로 쓴다."""
    if not isinstance(within, dict) or not within:
        return None
    period = within.get('기간')
    if isinstance(period, dict):
        start, end = period.get('시작') or '', period.get('종료') or ''
        period = (start if start == end else f"{start}~{end}") if (start or end) else None
    what = []
    for key, label in _WITHIN_LABELS.items():
        v = within.get(key)
        if not v:
            continue
        if key == '장르포함' and same_genres and sorted(map(str, v if isinstance(v, list) else [v])) == sorted(map(str, same_genres)):
            what.append("장르 선호장르와 동일")
        else:
            what.append(f"{label} {_join(v, max_items)}")
    if within.get('최소횟수'):
        what.append(f"{within['최소횟수']}회 이상")
    inner = " · ".join(([str(period)] if period else []) + what)
    return f"기간 내 시청({inner})" if inner else None


def _format_condition_parts(conditions, max_items=None):
    parts = []
    for key, label in _CONDITION_LABEL_MAP.items():
        val = (conditions or {}).get(key)
        if val:
            parts.append(f"{label}={_join(val, max_items)}" if isinstance(val, list) else f"{label}={val}")
    same = (conditions or {}).get('선호장르') if max_items else None
    within_part = _format_within_part((conditions or {}).get('기간내시청'), max_items, same if isinstance(same, list) else None)
    if within_part:
        parts.append(within_part)
    if (conditions or {}).get('분석출처'):
        parts.append(f"분석에서 가져온 집단({conditions['분석출처']})")
    return parts


def _format_top_n_part(top_n_cond):
    """🌟 [상위 N명 타겟팅] '상위N명'은 {"인원수": ?, "기준": ?} 형태의 dict라
    _format_condition_parts()의 일반 라벨 매핑(값을 그대로 이어붙이는 방식)으로는
    "상위 N명={'인원수': 100, ...}"처럼 보기 흉하게 나온다. 제외조건과 마찬가지로
    별도로 사람이 읽기 좋은 문장으로 바꾼다."""
    if not isinstance(top_n_cond, dict) or not top_n_cond.get('인원수'):
        return None
    metric_label = top_n_cond.get('기준') or '충성도/몰입도 복합점수'
    return f"상위 {top_n_cond['인원수']}명({metric_label} 기준)"


def _build_cond_str(conditions, empty_text, max_items=None):
    conditions = conditions or {}
    cond_parts = _format_condition_parts(conditions, max_items)
    exclude_cond = conditions.get('제외조건')
    if isinstance(exclude_cond, dict) and exclude_cond:
        exclude_parts = _format_condition_parts(exclude_cond, max_items)
        if exclude_parts:
            cond_parts.append(f"제외: {', '.join(exclude_parts)}")
    top_n_part = _format_top_n_part(conditions.get('상위N명'))
    if top_n_part:
        cond_parts.append(top_n_part)
    return ", ".join(cond_parts) if cond_parts else empty_text


def format_conditions_line(conditions, max_items=None):
    """max_items를 주면 화면 표시용 요약(긴 목록은 앞 몇 개 + '외 N개'). 안 주면 AI에 넘기는 전체 표기."""
    return _build_cond_str(conditions, "아직 확정된 조건 없음", max_items)


def format_target_summary(conditions, stats):
    cond_str = _build_cond_str(conditions, "조건 없음(전체 시청자)")

    stats = stats or {}
    stats_str = (
        f"대상자수 {stats.get('대상자수', 0)}명 (전체 시청자 {stats.get('전체시청자수', 0)}명 중), "
        f"평균 시청횟수 {stats.get('평균시청횟수', 0)}회(전체 평균 {stats.get('전체평균시청횟수', 0)}회), "
        f"평균 총 시청시간 {stats.get('평균총시청시간(분)', 0)}분(전체 평균 {stats.get('전체평균총시청시간(분)', 0)}분), "
        f"평균 시청유지율 {stats.get('평균시청유지율', 0)}%(전체 평균 {stats.get('전체평균시청유지율', 0)}%), "
        f"평균 시청콘텐츠수 {stats.get('평균시청콘텐츠수', 0)}개"
    )
    return f"[적용 조건] {cond_str}\n[집계] {stats_str}"
