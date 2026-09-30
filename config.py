# config.py
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

# 🌟 [DB 폴더 일원화] 모든 DB는 data/ 한 폴더에만 둔다. 시청이력은 가공 완료본을
# 월별 파일(시청_YYYY_MM.db)로, 콘텐츠 통계/직원 목록/대화 기록은 각각 파일 하나로.
DATA_DIR = os.path.join(base_dir, 'data')
DB_PATH = os.path.join(DATA_DIR, '대화기록.db')
CONTENT_DB_PATH = os.path.join(DATA_DIR, '콘텐츠통계.db')
EMPLOYEE_DB_PATH = os.path.join(DATA_DIR, '직원목록.db')

TARGET_CONDITION_FIELDS = [
    "성별", "나이대", "나이최소", "나이최대", "SO",
    "선호장르", "선호시청시간대", "선호채널", "선호메뉴",
    "최소시청횟수", "최근시청일이후",
    "최소시청유지율", "최소총시청시간_분", "최소시청콘텐츠수",
    "활동세그먼트", "콘텐츠명포함", "채널명포함",
    "기간내시청",
    "제외조건",
]

# 🌟 [SO 권역] 분석할 때 SO는 기본적으로 이 8개 권역으로 묶어 본다(순서 = 표/그래프 표시 순서).
# 세부 SO가 필요할 때만 "SO세부"로 나눠 본다. 여기 없는 SO(예: 업로더 '본부')는 이름 그대로 쓴다.
SO_REGIONS = {
    '대전': ['㈜씨엠비', '㈜씨엠비동대전방송'],
    '충청': ['㈜씨엠비충청방송'],
    '세종': ['㈜씨엠비세종방송'],
    '광주': ['㈜씨엠비광주방송', '㈜씨엠비광주동부방송'],
    '전남': ['㈜씨엠비전남방송'],
    '영등포': ['㈜씨엠비영등포방송'],
    '동대문': ['㈜씨엠비동대문방송'],
    '대구': ['㈜씨엠비대구방송', '㈜씨엠비수성방송'],
}
SO_TO_REGION = {so: region for region, sos in SO_REGIONS.items() for so in sos}
REGION_ORDER = list(SO_REGIONS)

# 🌟 [지표 정의집] 분석에서 쓰는 지표의 정의를 한 곳에 모은다. AI는 항상 이 정의로 계산하고 답변에 밝힌다
# (예전에는 정의가 프롬프트 여기저기에 흩어져 있어서 "신규 시청자"가 답변마다 다르게 계산됐다 - 647명/714명).
# 정의를 바꾸고 싶으면 여기만 고치면 분석 프롬프트에 그대로 반영된다.
METRIC_DEFINITIONS = {
    'MAU': "월별 고유 시청자수(R고객번호의 중복 제거 개수). SO별이면 그 SO권역에서 본 고유 시청자.",
    'DAU': "일별 고유 시청자수.",
    '재방문율': ("(전월 시청자 ∩ 이번 달 시청자) 수 ÷ 전월 시청자 수 × 100(%). 분모는 반드시 전월 시청자. "
              "권역별이면 전월 그 권역 시청자 중 이번 달에 그 권역에서도 본 사람 기준."),
    '신규 시청자': ("그 달 이전에 적재된 시청 기록이 플랫폼 전체에서 한 번도 없는 고객(권역과 무관). "
                "적재된 첫 달은 신규 판단이 불가하니 그 달의 신규는 계산하지 말고 그렇다고 밝힐 것. "
                "'그 권역에서 처음 본 고객'을 물으면 권역 신규라고 밝히고 따로 계산."),
    '이탈 시청자': "전월에는 시청했는데 이번 달에는 시청 기록이 없는 고객.",
    '시청완료율': "시청 유지율이 99.9 이상인 시청의 비율(%). 시청 유지율이 빈 값인 기록(러닝타임 정보 없음)은 분모·분자에서 제외.",
    '시청 유지율': "끝까지 본 정도(%). 빈 값인 기록은 평균 계산에서 제외.",
    '삭제 콘텐츠': "영상 단위 콘텐츠 성과를 볼 때는 '삭제 여부'가 'O'인 콘텐츠를 제외(시청 기록 자체는 고객 분석에 그대로 사용).",
}

ACTIVE_SEGMENT_DAYS = 14   # 이 일수 이내에 시청했으면 '활성'
DORMANT_SEGMENT_DAYS = 45  # 이 일수를 넘기면 '이탈위험' (그 사이는 '휴면')

PUSH_TITLE_MAX_LEN = 30
PUSH_BODY_MAX_LEN = 30

SMS_TITLE_MAX_LEN = 30   # "(광고)" 표기 포함
SMS_BODY_MAX_LEN = 300   # 무료수신거부 등 필수 안내 문구 포함


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
        # 🌟 [버그 수정 - 대화 전환 시 상태 누수] 앱푸시/SMS 카피 생성 버튼의 활성/비활성
        # 상태와 마지막으로 만든 카피 종류. 이 값들이 초기화 목록에 없으면, A 대화에서
        # 카피를 만든 뒤 B 대화로 전환해도 이 값이 그대로 남아 B에서 카피를 만든 적이
        # 없는데도 버튼이 비활성화된 것처럼 보이는 문제가 있었다.
        'push_copy_generated': False,
        'sms_copy_generated': False,
        'last_copy_type': 'push',
        'pending_copy_start': None,
    }


def _fresh_simple_chat_state(prefix):
    """📊 분석 / 📝 보고서처럼 phase 상태머신이 없는 단순 챗 탭 하나의 초기 상태.
    타겟팅 탭과 세션 상태를 완전히 분리해서 사이드바 세로 메뉴로 탭을 오가도
    서로 상태가 섞이지 않게 한다. 두 탭이 필요로 하는 상태 모양이 완전히
    같아서(메시지 목록 + 타이핑 애니메이션 플래그) prefix만 다르게 재사용한다."""
    from database.db_manager import new_conversation_id
    return {
        f'{prefix}_current_conversation_id': new_conversation_id(),
        f'{prefix}_messages': [],           # [{"role": ..., "text": ..., "chart": {...}(선택)}]
        f'{prefix}_greet_stream_pending': True,
        f'{prefix}_pending_user_text': None,
        f'{prefix}_stream_next': False,
    }


def _fresh_analysis_state():
    return _fresh_simple_chat_state('analysis')


def _fresh_report_state():
    return _fresh_simple_chat_state('report')


def init_session_state():
    """Streamlit 세션 상태 초기화 (앱 최초 진입 시 1회)."""
    if 'current_conversation_id' not in st.session_state:
        for k, v in _fresh_conversation_state().items():
            st.session_state[k] = v
    if 'analysis_current_conversation_id' not in st.session_state:
        for k, v in _fresh_analysis_state().items():
            st.session_state[k] = v
    if 'report_current_conversation_id' not in st.session_state:
        for k, v in _fresh_report_state().items():
            st.session_state[k] = v
    if 'active_feature' not in st.session_state:
        st.session_state.active_feature = 'targeting'  # 'targeting' | 'analysis' | 'report'


def start_new_conversation():
    """사이드바 '+ 새 대화'에서 호출 - 현재 세션을 완전히 새 대화 상태로 초기화."""
    for k, v in _fresh_conversation_state().items():
        st.session_state[k] = v


def start_new_analysis_conversation():
    """사이드바 '+ 새 대화'(📊 분석 탭)에서 호출."""
    for k, v in _fresh_analysis_state().items():
        st.session_state[k] = v


def load_analysis_conversation_into_session(conv_state):
    """DB에서 불러온 분석 대화 상태(dict)를 세션에 그대로 반영."""
    st.session_state.analysis_current_conversation_id = conv_state['id']
    st.session_state.analysis_messages = conv_state['messages']
    st.session_state.analysis_greet_stream_pending = False
    st.session_state.analysis_stream_next = False


def start_new_report_conversation():
    """사이드바 '+ 새 대화'(📝 보고서 탭)에서 호출."""
    for k, v in _fresh_report_state().items():
        st.session_state[k] = v


def load_report_conversation_into_session(conv_state):
    """DB에서 불러온 보고서 대화 상태(dict)를 세션에 그대로 반영."""
    st.session_state.report_current_conversation_id = conv_state['id']
    st.session_state.report_messages = conv_state['messages']
    st.session_state.report_greet_stream_pending = False
    st.session_state.report_stream_next = False


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
    # 🌟 [버그 수정 - 대화 전환 시 상태 누수] 지금은 대화별로 "카피를 만들었는지" 자체를
    # DB에 따로 저장하지 않으므로(카피 텍스트 자체만 push_copy_result로 저장됨), 대화를
    # 불러올 때마다 이 대화가 새로 시작하는 것처럼 항상 초기화한다. 그렇지 않으면 방금 전
    # 대화에서 만든 값이 그대로 남아 지금 불러온 이 대화의 버튼 상태를 오염시킨다.
    st.session_state.push_copy_generated = False
    st.session_state.sms_copy_generated = False
    st.session_state.last_copy_type = 'push'
    st.session_state.pending_copy_start = None
