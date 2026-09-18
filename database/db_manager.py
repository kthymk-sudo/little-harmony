# database/db_manager.py
# ============================================================
# 🌟 [모듈화] 예전에는 이 파일 하나에 DB 연결/적재/집계/대화 저장까지 20개
# 가까운 함수가 전부 들어있었다. 파일이 너무 길어지면 한 군데 실수가 전체에
# 영향을 줄 위험이 커진다고 판단해, 실제 로직은 아래 파일들로 분리했다.
#   - database/connection.py         : DB 연결, 데이터 버전 확인
#   - database/upsert.py             : 시청내역/직원리스트 적재(upsert)
#   - database/loader.py             : 캐시된 데이터 로딩
#   - database/audience.py           : 오디언스 집계/타겟 필터링
#   - database/formatting.py         : 조건/집계 결과를 사람이 읽는 문장으로 변환
#   - database/conversation_store.py : 대화 저장/불러오기/삭제
#
# 이 파일은 기존 코드(`from database.db_manager import ...`)가 하나도
# 수정될 필요 없도록, 위 모듈들의 함수를 그대로 다시 내보내기만 하는
# 창구(facade) 역할만 한다. 새로운 로직을 여기에 직접 추가하지 말 것 -
# 알맞은 위 모듈에 추가한 뒤 여기서 import만 추가하면 된다.
# ============================================================
from database.connection import _connect, get_data_version, get_loaded_periods
from database.upsert import upsert_to_db, _upsert_table
from database.loader import (
    load_from_db, load_history_period, load_employee_list, has_history_data,
    optimize_db, _load_from_db_cached, _load_employee_cached, _load_history_period_cached,
)
from database.audience import (
    build_audience_db, build_audience_profile, filter_by_period, summarize_profile_context,
    summarize_segment_insight, format_segment_insight_reply, apply_target_conditions,
    _matching_ids_by_keyword, _as_list, _to_num, _filter_by_conditions,
    _parse_recent_date_condition, _normalize_field_name,
)
from database.formatting import (
    format_conditions_line, format_target_summary,
    _CONDITION_LABEL_MAP, _format_condition_parts, _build_cond_str,
)
from database.conversation_store import (
    new_conversation_id, make_conversation_title, save_conversation, rename_conversation,
    list_conversations, load_conversation, delete_conversation, _ensure_conversation_table,
)

__all__ = [
    "_connect", "get_data_version", "get_loaded_periods",
    "upsert_to_db", "_upsert_table",
    "load_from_db", "load_history_period", "load_employee_list", "has_history_data", "optimize_db",
    "build_audience_db", "build_audience_profile", "filter_by_period", "summarize_profile_context",
    "summarize_segment_insight", "format_segment_insight_reply", "apply_target_conditions",
    "format_conditions_line", "format_target_summary",
    "new_conversation_id", "make_conversation_title", "save_conversation", "rename_conversation",
    "list_conversations", "load_conversation", "delete_conversation",
]
