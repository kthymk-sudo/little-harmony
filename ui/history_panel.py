# ui/history_panel.py
# ============================================================
# 기능: 저장된 타겟 이력 목록을 보여주고, 하나를 골라 다시 불러와
# 이어서 작업(카피 재작성 등)할 수 있게 하는 화면.
#
# 참고: 대화 자체(1단계 채팅 로그)는 저장하지 않고, 최종 확정된
# 조건/대상자 스냅샷/근거/카피만 저장한다. 불러오면 대상자 목록은
# 저장 당시의 R고객번호 스냅샷을 현재 프로필과 다시 맞춰서 보여준다
# (그 사이 데이터가 갱신되어 일부 대상자가 빠질 수 있음).
# ============================================================
import streamlit as st
from database.db_manager import (
    load_target_history_list, load_target_history_detail,
    delete_target_history, format_target_summary,
)


def load_history_into_session(history_id, profile_df):
    """저장된 이력 하나를 현재 세션에 불러온다 (순수 로직, 단위 테스트 가능)."""
    detail = load_target_history_detail(history_id)
    if detail is None:
        return None

    member_ids = set(detail['member_ids'])
    restored_df = profile_df[profile_df['R고객번호'].astype(str).isin(member_ids)]

    return {
        'target_conditions': detail['conditions'],
        'target_result_df': restored_df,
        'target_result_stats': detail['stats'],
        'target_summary_str': format_target_summary(detail['conditions'], detail['stats']),
        'target_reasoning': detail['reasoning'],
        'push_copy_result': detail['push_copy'],
        'push_chat_history': [{"role": "ai", "text": detail['push_copy']}] if detail['push_copy'] else [],
        'active_history_id': detail['id'],
    }


def render_history_panel(profile_df):
    st.subheader("4. 저장된 타겟 이력")
    st.caption("이전에 저장해둔 타겟을 다시 불러와 카피를 이어서 작성하거나 참고할 수 있습니다.")

    history_df = load_target_history_list()
    if history_df.empty:
        st.info("아직 저장된 타겟이 없습니다. 2단계에서 타겟을 확정한 뒤 저장해보세요.")
        return

    for _, row in history_df.iterrows():
        with st.container(border=True):
            col_info, col_load, col_delete = st.columns([6, 1, 1])
            with col_info:
                st.write(f"**{row['name']}**")
                st.caption(f"대상자 {row['대상자수']}명 · 저장일 {row['created_at']} · 최종수정 {row['updated_at']}")
            with col_load:
                if st.button("불러오기", key=f"load_{row['id']}"):
                    restored = load_history_into_session(row['id'], profile_df)
                    if restored:
                        for k, v in restored.items():
                            st.session_state[k] = v
                        st.success(f"'{row['name']}' 을(를) 불러왔습니다. 2~3단계 탭에서 이어서 작업하세요.")
                        st.rerun()
            with col_delete:
                if st.button("삭제", key=f"delete_{row['id']}"):
                    delete_target_history(row['id'])
                    if st.session_state.active_history_id == row['id']:
                        st.session_state.active_history_id = None
                    st.rerun()
