# config.py
# ============================================================
# 하모니펄스(harmony_pulse) 프로젝트 설정 파일
# 기존 '하모니' 프로젝트의 config.py 구조(환경변수 기반 API 키, 실행 위치
# 기준 DB 경로 고정)를 참고하되, 이 프로젝트는 완전히 독립된 새 폴더/새 DB를 사용한다.
#
# 🌟 [개편] 탭으로 나뉘어 있던 상태를 "하나의 대화 스레드" 상태로 통합.
# 대화 하나(session_state.messages)가 타겟 설정 -> 확정/근거 -> 카피 작성까지
# 이어지며, phase 값으로 지금 어느 단계인지를 구분한다.
# ============================================================
import os
import sys
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(base_dir, 'harmony_pulse_data.db')

# 🌟 [프롬프트/타겟팅 고도화 - 1차] 기존 인구통계/장르 축에 더해, 이미 집계되어 있었지만
# 조건으로는 쓰이지 않던 '몰입도' 축(시청 유지율/총시청시간/시청콘텐츠수)을 추가해
# 더 세밀한 타겟팅(예: "많이 보되 끝까지 보는 충성 시청자")이 가능하도록 함.
#
# 🌟 [타겟팅 고도화 - 2차] 아래 축을 추가로 확장:
#  - 선호채널/선호메뉴: 장르/시간대와 동일한 방식(최빈값)으로 집계되는 신규 축
#  - 나이최소/나이최대: 나이대(10년 단위 버킷) 대신 정확한 나이 범위 지정
#  - 활동세그먼트: 최근시청일/시청횟수를 기반으로 자동 계산되는 활성/휴면/이탈위험 세그먼트
#  - 콘텐츠명포함/채널명포함: 영상명/채널명에 특정 키워드가 포함된 시청 이력이 있는 사람만
#    (대략적인 이름으로 행동 기반 타겟팅 - 별도 선택 UI 없이 자연어로 지정)
#  - 제외조건: 위 필드들과 동일한 구조를 그대로 재사용해 "~는 빼고" 조건을 표현하는
#    중첩 dict (예: 특정 콘텐츠를 이미 본 사람을 제외 -> 제외조건.콘텐츠명포함)
TARGET_CONDITION_FIELDS = [
    "성별", "나이대", "나이최소", "나이최대", "SO",
    "선호장르", "선호시청시간대", "선호채널", "선호메뉴",
    "최소시청횟수", "최근시청일이후",
    "최소시청유지율", "최소총시청시간_분", "최소시청콘텐츠수",
    "활동세그먼트", "콘텐츠명포함", "채널명포함",
    "제외조건",
]

# 🌟 [타겟팅 고도화 - 2차] 활동세그먼트(활성/휴면/이탈위험) 자동 분류 기준(일).
# 기준 시점은 실행 시각(오늘)이 아니라, 업로드된 시청이력 데이터 자체의 최신 시청일이다
# (오래된 과거 데이터를 올려도 "오늘 기준"으로 전부 이탈위험 처리되는 것을 방지).
ACTIVE_SEGMENT_DAYS = 14   # 이 일수 이내에 시청했으면 '활성'
DORMANT_SEGMENT_DAYS = 45  # 이 일수를 넘기면 '이탈위험' (그 사이는 '휴면')

PUSH_TITLE_MAX_LEN = 30
PUSH_BODY_MAX_LEN = 30


def _fresh_conversation_state():
    """새 대화 하나의 초기 상태 (새 대화 시작 / 최초 진입 시 공용으로 사용)."""
    from database.db_manager import new_conversation_id
    return {
        'current_conversation_id': new_conversation_id(),
        'messages': [],                # [{"role": "user"/"assistant", "text": "..."}]
        'phase': 'targeting',          # 'targeting' -> 'copywriting'
        'target_conditions': {},
        'target_result_df': None,
        'target_result_stats': {},
        'target_summary_str': "",
        'target_reasoning': "",
        'push_copy_result': "",
        # 🌟 [타이핑 효과용] 방금 생성된 AI 메시지 1개에만 타이핑 애니메이션을 적용하기 위한 플래그.
        # 과거 메시지(재접속/대화 전환 시 복원된 메시지)에는 절대 적용하지 않는다.
        'stream_next': False,
        'greet_stream_pending': True,
        # 🌟 [응답 순서 개선용] 사용자 메시지를 먼저 화면에 그린 뒤, 다음 rerun에서
        # 이 값이 있으면 그때 AI 응답을 생성한다 (사용자 메시지 즉시 표시를 위함).
        'pending_user_text': None,
    }


def init_session_state():
    """Streamlit 세션 상태 초기화 (앱 최초 진입 시 1회)."""
    if 'current_conversation_id' not in st.session_state:
        for k, v in _fresh_conversation_state().items():
            st.session_state[k] = v
    # 🌟 [시청기간 설정] 대화 상태와 별개로 세션 전체에서 유지되는 데이터 범위 설정.
    # '새 대화'를 시작해도 초기화되지 않도록 _fresh_conversation_state()가 아닌
    # 별도 블록에서 최초 1회만 기본값을 넣는다.
    if 'use_period_filter' not in st.session_state:
        st.session_state.use_period_filter = False
        st.session_state.period_start = None
        st.session_state.period_end = None


def start_new_conversation():
    """사이드바 '+ 새 대화'에서 호출 - 현재 세션을 완전히 새 대화 상태로 초기화."""
    for k, v in _fresh_conversation_state().items():
        st.session_state[k] = v


def load_conversation_into_session(conv_state):
    """DB에서 불러온 대화 상태(dict)를 세션에 그대로 반영."""
    st.session_state.current_conversation_id = conv_state['id']
    st.session_state.messages = conv_state['messages']
    st.session_state.phase = conv_state['phase']
    st.session_state.target_conditions = conv_state['conditions']
    st.session_state.target_result_stats = conv_state['stats']
    st.session_state.target_reasoning = conv_state['reasoning']
    st.session_state.push_copy_result = conv_state['push_copy']
    st.session_state.target_summary_str = ""  # 프로필 데이터를 받은 뒤 화면단에서 다시 계산
    st.session_state._pending_member_ids = conv_state['member_ids']  # target_result_df 복원용 임시 저장
    # 저장된 대화를 불러올 때는 과거 메시지이므로 타이핑 애니메이션을 절대 재생하지 않는다
    st.session_state.stream_next = False
    st.session_state.greet_stream_pending = False
