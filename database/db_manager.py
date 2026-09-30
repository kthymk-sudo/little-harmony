# database/db_manager.py
# ============================================================
# 🌟 [모듈화] 실제 로직은 database/ 아래 파일들에 나뉘어 있다.
#   - connection.py         : DB 연결, 데이터 버전 확인
#   - upsert.py             : 시청내역(가공 후 월별)/직원리스트/콘텐츠 통계 적재
#   - loader.py             : 필요한 달 파일만 꺼내는 캐시된 데이터 로딩
#   - audience.py           : 오디언스 집계/타겟 필터링
#   - formatting.py         : 조건/집계 결과를 사람이 읽는 문장으로 변환
#   - conversation_store.py : 대화 저장/불러오기/삭제
#
# 이 파일은 다른 곳이 `from database.db_manager import ...`로 쓰는 이름만 다시 내보내는 창구다.
# 새 로직은 위 모듈에 추가하고, 여기에는 import만 추가한다.
# ============================================================
from database.connection import get_data_version, get_loaded_periods
from database.upsert import upsert_to_db
from database.loader import load_history_period, load_content, has_history_data
from database.audience import (
    build_audience_profile, summarize_profile_context,
    summarize_segment_insight, format_segment_insight_reply, apply_target_conditions,
    summarize_content_ranking, format_content_ranking_reply,
    summarize_group_breakdown, format_group_breakdown_reply,
    _filter_by_conditions, _normalize_field_name, describe_term_matches,
    describe_period_warnings, data_period_line,
)
from database.formatting import format_conditions_line, format_target_summary
from database.conversation_store import (
    new_conversation_id, make_conversation_title, save_conversation, rename_conversation,
    list_conversations, load_conversation, delete_conversation,
)
