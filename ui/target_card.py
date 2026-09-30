# ui/target_card.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "타겟 확정 안내문 + 확정 버튼"과 "확정된 타겟
# 결과 카드(대상자 수/엑셀 다운로드/카피 작성 버튼)"를 그리는 부분만 분리했다.
# 대화 자동저장(autosave)도 이 카드들과 함께 호출되는 흐름이라 같이 옮겼다.
# ============================================================
import io
import streamlit as st
from ai_engine.gemini_api import generate_target_reasoning, is_api_error
from services.copy_service import is_over_limit, SHORTEN_REQUEST
from database.db_manager import (
    apply_target_conditions, format_target_summary, format_conditions_line,
    save_conversation, make_conversation_title,
)


def _build_customer_id_excel(df):
    """캠페인 업로드 양식(R-고객번호 단일 컬럼)에 맞춘 엑셀 바이트 생성."""
    buffer = io.BytesIO()
    export_df = df[['R고객번호']].astype(str).rename(columns={'R고객번호': 'R-고객번호'})
    export_df.to_excel(buffer, index=False, sheet_name='이웃고객관리목록')
    return buffer.getvalue()


def autosave():
    member_ids = (
        st.session_state.target_result_df['R고객번호'].astype(str).tolist()
        if st.session_state.target_result_df is not None else []
    )
    first_user_text = next((m['text'] for m in st.session_state.messages if m['role'] == 'user'), "")
    saved = save_conversation(
        conv_id=st.session_state.current_conversation_id,
        title=make_conversation_title(first_user_text),
        phase=st.session_state.phase,
        messages=st.session_state.messages,
        conditions=st.session_state.target_conditions,
        stats=st.session_state.target_result_stats,
        member_ids=member_ids,
        reasoning=st.session_state.target_reasoning,
        push_copy=st.session_state.push_copy_result,
    )
    if not saved:
        st.session_state.save_failed = True  # main.py가 다음 화면에서 경고를 띄운다


def autosave_simple(feature):
    """분석/보고서 탭 자동저장(phase·조건·타겟이 없는 단순 챗). 상태 키: '{feature}_messages', '{feature}_current_conversation_id'."""
    messages = st.session_state[f"{feature}_messages"]
    first_user_text = next((m['text'] for m in messages if m['role'] == 'user'), "")
    if not save_conversation(
        conv_id=st.session_state[f"{feature}_current_conversation_id"],
        title=make_conversation_title(first_user_text, feature=feature),
        phase='', messages=messages, conditions={}, stats={}, member_ids=[], reasoning="", push_copy="",
        feature=feature,
    ):
        st.session_state.save_failed = True  # main.py가 다음 화면에서 경고를 띄운다


def render_target_card():
    """확정된 타겟 결과 카드(대상자 수 / 엑셀 다운로드 / 카피 작성 버튼)를 그린다.
    🌟 [UX 고도화] 이 카드를 메시지 목록 뒤에 고정으로 그리지 않고, 타겟이 확정된
    바로 그 메시지 자리에 끼워 넣어(ui/chat_app.py 참고), 이후 이어지는 카피 대화가
    이 카드 "아래로" 자연스럽게 쌓이도록 한다."""
    if st.session_state.target_result_df is None:
        return
    with st.container(border=True):
        stats = st.session_state.target_result_stats or {}
        m1, m2 = st.columns(2)
        m1.metric("확정된 대상자 수", f"{stats.get('대상자수', 0):,}명")
        total_n = stats.get('전체시청자수', 0)
        share = f"{stats.get('대상자수', 0) / total_n * 100:.1f}%" if total_n else "-"
        m2.metric("전체 시청자 대비 비중", share)
        if stats.get('전체평균시청유지율'):
            st.caption(
                f":material/monitoring: 이 타겟의 평균 시청유지율 {stats.get('평균시청유지율', 0)}% "
                f"(전체 평균 {stats.get('전체평균시청유지율', 0)}%) · "
                f"평균 총시청시간 {stats.get('평균총시청시간(분)', 0)}분 "
                f"(전체 평균 {stats.get('전체평균총시청시간(분)', 0)}분)"
            )
        show_cols = [c for c in ['R고객번호', '성별', '나이', '시청자SO', '선호장르', '선호시청시간대',
                                  '시청콘텐츠수', '평균시청유지율']
                     if c in st.session_state.target_result_df.columns]
        with st.expander("대상자 상세 보기"):
            st.dataframe(st.session_state.target_result_df[show_cols], width='stretch')

        with st.container(border=True):
            st.caption("R고객번호 엑셀 다운로드")
            st.download_button(
                "R고객번호 엑셀 다운로드 (캠페인 업로드 양식)",
                icon=":material/download:",
                data=_build_customer_id_excel(st.session_state.target_result_df),
                file_name="레인보우TV_이웃고객관리목록.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )

        # 🌟 [에러 오염 방지] render_confirm_bar에서 근거 생성이 실패하면 target_reasoning을
        # 빈 값으로 남겨둔다(에러 문구를 정상 근거처럼 저장하지 않기 위해). 그 상태로
        # 대화를 나중에 다시 열어봤을 때도 왜 근거가 없는지 알 수 있도록 안내한다.
        if not st.session_state.get('target_reasoning'):
            st.caption(":material/warning: AI 근거 설명 생성에 실패했었습니다. 채팅창에 메시지를 입력하면 조건이 다시 처리되면서 근거도 함께 갱신됩니다.")

        col_push, col_sms = st.columns(2)
        with col_push:
            if st.button(
                "앱푸시 카피 작성", icon=":material/smartphone:",
                disabled=st.session_state.get('push_copy_generated', False),
                width='stretch',
            ):
                st.session_state.pending_copy_start = 'push'
                st.rerun()
        with col_sms:
            if st.button(
                "SMS 문자 작성", icon=":material/sms:",
                disabled=st.session_state.get('sms_copy_generated', False),
                width='stretch',
            ):
                st.session_state.pending_copy_start = 'sms'
                st.rerun()

        if st.session_state.phase == 'copywriting':
            st.caption("아래 채팅창에 원하는 방향을 입력하면 방금 만든 카피를 다시 다듬어드립니다. (예:이벤트, 친근한 느낌)")
            # 🌟 [글자수 맞춰 다시 쓰기] 제한을 넘은 버전이 있으면 버튼 한 번으로 줄여서 다시 쓰게 한다
            if is_over_limit(st.session_state.get('push_copy_result'), st.session_state.get('last_copy_type', 'push')):
                if st.button("글자수 맞춰 다시 쓰기", icon=":material/content_cut:", width='stretch', key="btn_shorten_copy"):
                    st.session_state.messages.append({"role": "user", "text": SHORTEN_REQUEST})
                    st.session_state.pending_user_text = SHORTEN_REQUEST
                    st.rerun()
            if st.button("타겟 조건 다시 설정하기", icon=":material/tune:", width='stretch'):
                st.session_state.phase = 'targeting'
                st.session_state.messages.append({
                    "role": "assistant",
                    "text": "네, 타겟 조건을 다시 조정해볼까요? 어떤 부분을 바꾸고 싶으신지 말씀해주세요.",
                })
                st.session_state.stream_next = True
                autosave()
                st.rerun()

def render_confirm_bar(profile_df, db_audience):
    """'이 조건으로 타겟 확정하기' 안내문 + 버튼을 그린다.
    🌟 [UX 고도화] 이 액션 바를 항상 화면 맨 아래(카드보다도 아래)에 고정으로 그리면,
    한 번 타겟을 확정한 뒤에는 버튼이 카드 밑에 다시 나타나 어색해 보인다. 그래서
    이미 확정된 타겟(카드)이 있을 때는 이 함수를 카드 "바로 위"(anchor 위치)에서
    호출하고, 아직 한 번도 확정한 적 없을 때만 메시지 목록 맨 끝에서 호출한다
    (ui/chat_app.py 참고). 조건을 더 이야기해서 바꾼 뒤 다시 눌러 재확정하는 것도
    이 함수 하나로 그대로 지원된다."""
    conditions = st.session_state.target_conditions
    short_line, full_line = format_conditions_line(conditions, max_items=3), format_conditions_line(conditions)
    st.info(f"현재까지 파악된 조건: {short_line}", icon=":material/sell:")
    if full_line != short_line:  # 긴 목록(장르 등)을 줄여 보여줬으면 전체는 펼쳐서 볼 수 있게
        with st.expander("조건 전체 보기"):
            st.write(full_line)
    # 🌟 [예상 인원 미리 보기] 확정을 누르기 전에도 지금 조건에 몇 명이 해당하는지 바로 보여준다
    try:
        _, preview_stats = apply_target_conditions(profile_df, conditions, db_audience)
        n, total = preview_stats.get('대상자수', 0), preview_stats.get('전체시청자수', 0)
        share = f" (전체 {total:,}명의 {n / total * 100:.1f}%)" if total else ""
        if n == 0:
            st.warning(f"지금 조건에 해당하는 분이 **0명**이에요. 조건을 조금 넓혀볼까요?{share}", icon=":material/warning:")
        else:
            st.caption(f":material/group: 지금 조건의 예상 인원: **{n:,}명**{share}")
    except Exception:  # 미리 보기는 참고용이라 실패해도 확정 흐름은 그대로
        pass
    if st.button("이 조건으로 타겟 확정하기", icon=":material/check_circle:", type="primary"):
        with st.spinner("타겟을 계산하고 근거를 정리하는 중..."):
            target_df, stats = apply_target_conditions(profile_df, st.session_state.target_conditions, db_audience)
            summary_str = format_target_summary(st.session_state.target_conditions, stats)
            visible = {k: v for k, v in st.session_state.target_conditions.items() if not k.startswith('__')}
            reasoning = generate_target_reasoning(str(visible), str(stats))  # '__' 키(가져온 고객번호 목록)는 AI에 안 보낸다

        # 🌟 [에러 오염 방지] 대상자 수 계산(target_df/stats)은 AI 호출과 무관하게 항상
        # 성공하므로, 근거(reasoning) 생성이 실패했더라도 결과 카드(엑셀 다운로드 포함)는
        # 그대로 보여준다. 다만 실패 문구를 정상 근거처럼 저장/전달하면 (1) 다음 대화
        # 턴 프롬프트에 "AI: ⚠️..."로 섞여 들어가고 (2) 이후 카피 생성 프롬프트에도
        # 에러 문구가 그대로 들어가 버리므로, 반드시 여기서 걸러낸다.
        st.session_state.target_result_df = target_df
        st.session_state.target_result_stats = stats
        st.session_state.target_summary_str = summary_str
        # 🌟 [카피 타입 분리] 새로 타겟을 확정할 때마다 앱푸시/SMS 버튼을 다시 눌러
        # 만들 수 있도록 두 생성 여부 플래그를 초기화한다.
        st.session_state.push_copy_generated = False
        st.session_state.sms_copy_generated = False

        if is_api_error(reasoning):
            st.session_state.target_reasoning = ""
            autosave()
            st.error(
                f"대상자 수 계산은 완료됐지만, AI 근거 설명 생성에는 실패했습니다.\n\n{reasoning}\n\n"
                "대상자 목록 다운로드는 아래 카드에서 바로 가능합니다. 근거 설명은 채팅창에 "
                "메시지를 입력해 조건을 다시 처리하면 함께 다시 생성됩니다."
            )
            # 🌟 실패 시에는 rerun하지 않는다 - rerun하면 이 안내가 화면에서 바로 사라져
            # 실무자가 무슨 일이 있었는지 못 보고 지나칠 수 있다. 이번 실행 안에서
            # 아래쪽 렌더링(결과 카드)이 이어서 그려지므로 카드도 함께 바로 보인다.
            return

        st.session_state.target_reasoning = reasoning
        st.session_state.messages.append({
            "role": "assistant",
            "text": f"총 {stats.get('대상자수', 0)}명이 이 조건에 해당합니다.\n\n{reasoning}",
        })
        st.session_state.stream_next = True
        autosave()
        st.rerun()