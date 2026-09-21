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


def _format_condition_parts(conditions):
    parts = []
    for key, label in _CONDITION_LABEL_MAP.items():
        val = (conditions or {}).get(key)
        if val:
            if isinstance(val, list):
                parts.append(f"{label}={','.join(map(str, val))}")
            else:
                parts.append(f"{label}={val}")
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


def _build_cond_str(conditions, empty_text):
    conditions = conditions or {}
    cond_parts = _format_condition_parts(conditions)
    exclude_cond = conditions.get('제외조건')
    if isinstance(exclude_cond, dict) and exclude_cond:
        exclude_parts = _format_condition_parts(exclude_cond)
        if exclude_parts:
            cond_parts.append(f"제외: {', '.join(exclude_parts)}")
    top_n_part = _format_top_n_part(conditions.get('상위N명'))
    if top_n_part:
        cond_parts.append(top_n_part)
    return ", ".join(cond_parts) if cond_parts else empty_text


def format_conditions_line(conditions):
    return _build_cond_str(conditions, "아직 확정된 조건 없음")


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
