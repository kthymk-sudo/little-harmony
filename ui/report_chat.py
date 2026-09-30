# ui/report_chat.py
# ============================================================
# 📝 보고서 탭 화면. 📊 분석 탭과 같은 단순 챗 패턴(phase 없음)이다.
# 🌟 [활동 보고서 정리] SO별 활동 보고서를 하나씩 붙여넣으면 트리로 정리해 쌓고, 쌓인 보고서로 취합 방향을 대화로
# 의논해 취합안(키워드 위주 등)을 만든다. 알마인드 파일은 화면 아래 패널에서 최종본 하나만 내려받는다.
# 시청 데이터 집계를 요청했을 때만 차트/표가 추가로 붙는다.
# ============================================================
import re

import streamlit as st
from services.report_service import (
    process_report_turn, read_attachments, collect_so_reports, collect_materials, merged_title, latest_draft,
    draft_is_stale,
)
from utils.file_reader import FILE_TYPES
from ui.chart_render import render_chart_card
from ui.chat_styles import inject_chat_css, render_message
from database.db_manager import save_simple_conversation
from ui import job_runner
from utils.mindmap_svg import mindmap_svg
from ui.target_card import autosave_simple
from utils.emm_export import build_emm_from_nodes, merge_report_trees, parse_report_tree, strip_tree_lines


def _safe_filename(text):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', text).strip('_') or "보고서"


def _use_key(so):
    return f"merge_use_{st.session_state.get('report_current_conversation_id')}_{so}"


def _excluded_so(messages):
    """취합 체크박스에서 뺀 SO(AI가 취합안을 만들 때도 재료에서 뺀다)."""
    return [r['SO'] for r in collect_so_reports(messages) if st.session_state.get(_use_key(r['SO'])) is False]


def _material_key(index):
    return f"mat_use_{st.session_state.get('report_current_conversation_id')}_{index}"


def _excluded_materials(messages):
    """체크박스에서 뺀 분석 자료의 메시지 번호."""
    return [m['index'] for m in collect_materials(messages) if st.session_state.get(_material_key(m['index'])) is False]


_DEPTHS = {"한눈에": 2, "3단계": 3, "전체": 9}


def _render_merge_panel(messages):
    """🌟 [현재 알마인드] 올린 자료를 한 줄로 보여주고, 지금 알마인드가 어떤 구조인지 그림으로 한눈에 보여주며 최종 파일을 딱 하나 내려받게 한다.
    대화로 만든 취합안이 있으면 그것을, 없으면 SO 보고서를 가지 하나씩 묶은 기본 취합을 그린다(취합안을 만들면 그것이 최종본이 된다).
    구성·내용을 고치는 일은 전부 대화로 하므로 "어느 안을 받을지" 고르는 컨트롤은 없다. 자료를 빼는 체크박스는 '자료 관리'에만 있다."""
    reports, materials = collect_so_reports(messages), collect_materials(messages)
    if not reports and not materials:
        return
    conv = st.session_state.get('report_current_conversation_id')
    with st.container(border=True):
        st.markdown("**:material/account_tree: 현재 알마인드**")
        st.caption(f"자료 {len(reports) + len(materials)}개: " + " · ".join([r['SO'] for r in reports] + [m['제목'] for m in materials]))

        with st.expander("자료 관리", icon=":material/tune:"):
            def _label(r):
                note = f" · 원문 표기 '{r['원본이름']}'" if r['원본이름'] != r['SO'] else ""
                note += " · 다시 올려 최신본으로 교체됨" if r['교체'] else ""
                return f"{r['SO']} ({len(r['nodes'])}개 항목){note}"

            picked = [r for r in reports if st.checkbox(_label(r), value=True, key=_use_key(r['SO']))]
            for m in materials:
                st.checkbox(f"{m.get('종류', '분석')} 자료 · {m['제목']}", value=True, key=_material_key(m['index']))
            st.caption("체크를 풀면 그 자료는 이후 취합·요약에서 빠져요. 분석·첨부 자료는 대화로 취합안을 만들 때 반영돼요.")
            if any(r['SO'].startswith("SO 미확인") for r in reports):
                st.caption(":material/warning: SO를 알 수 없는 보고서가 있어요. 다시 올리면서 'OO 보고서야'라고 SO를 알려주세요.")

        draft = latest_draft(messages)
        if draft:
            if draft_is_stale(messages):
                st.warning("취합안을 만든 뒤 자료가 새로 담기거나 교체됐어요. 그 내용은 이 알마인드에 반영되지 않았으니, "
                           "대화로 '취합안 다시 만들어줘'라고 요청해 주세요.", icon=":material/warning:")
            nodes = parse_report_tree(draft['text'])
            title = nodes[0][1]
        elif len(picked) >= 2:
            key_suffix = "_".join(r['SO'] for r in picked)
            title = st.text_input("취합본 제목 (알마인드 중심 토픽)", value=merged_title(picked), key=f"merge_title_{key_suffix}").strip() or merged_title(picked)
            nodes = merge_report_trees([r['nodes'] for r in picked])
        elif picked:
            nodes = picked[0]['nodes']
            title = nodes[0][1]
        else:
            st.caption("'자료 관리'에서 자료를 하나 이상 선택하거나, 대화로 '취합안 만들어줘'라고 해 주세요.")
            return

        view = st.segmented_control("보기", list(_DEPTHS), default="한눈에", key=f"map_view_{conv}", label_visibility="collapsed") or "한눈에"
        svg, hidden = mindmap_svg(nodes, title=title, max_depth=_DEPTHS[view])
        st.markdown(f'<div class="hp-mindmap">{svg}</div>', unsafe_allow_html=True)
        st.caption(f"{len(nodes)}개 항목 · " + (f"'{view}'로 그려서 {hidden}개 항목이 숨겨져 있어요. 위에서 '3단계'나 '전체'를 눌러 보세요. " if hidden else "모든 항목을 보여줘요. ")
                   + "고치고 싶으면 대화로 말씀해 주세요(예: 'SO가 가장 큰 가지로', '요약해서 다시 만들어줘', '조회수를 맨 위로').")
        with st.expander("글로 보기", icon=":material/notes:"):
            st.code("\n".join("  " * d + t for d, t in nodes), language=None)
        st.download_button("최종 알마인드 다운로드 (.emm)", icon=":material/download:", data=build_emm_from_nodes(nodes, title),
                           file_name=f"{_safe_filename(title)}.emm", mime="application/octet-stream", key="emm_final")


def _has_tree(turn):
    return turn["role"] == "assistant" and len(parse_report_tree(turn["text"])) >= 3


def _tree_done_sentence(turn):
    """트리 답변에 붙은 글이 없을 때 말풍선에 보여줄 한 줄."""
    nodes = parse_report_tree(turn["text"])
    what = "취합안을 만들었어요" if turn.get("draft") else f"'{nodes[0][1]}' 정리를 마쳤어요"
    return f"{what} ({len(nodes)}개 항목). 구조는 아래 '현재 알마인드'에서 확인해 주세요."


def _render_preview(text):
    """🌟 [알마인드 미리보기] 의논 중 보여준 예시 트리를 알마인드 가지형 그림으로 보여준다(글이 아니라 모양으로 볼 수 있게)."""
    nodes = parse_report_tree(text)
    svg, _ = mindmap_svg(nodes, max_depth=3)
    if svg:
        st.markdown(f'<div class="hp-mindmap">{svg}</div>', unsafe_allow_html=True)
        st.caption("예시 그림이에요. 마음에 들면 '이 구성으로 취합안 만들어줘'라고 말씀해 주세요.")


def render_report_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title(":material/description: 보고서")
    st.caption("글, 사진, 파일을 올리면 AI가 내용을 분석해 취합·요약하고, 대화로 구성을 다듬어 최종 알마인드 한 개로 받아요.")

    if not st.session_state.report_messages:
        greeting = (
            "안녕하세요! 활동 보고서 글을 붙여넣거나, 알마인드·엑셀·워드·한글·CSV·텍스트·사진·PDF 파일을 올려 주세요. "
            "올린 자료를 읽고 트리로 정리해 두었다가, '올린 자료를 종합해서 요약해줘', '중복되는 건 묶어서 한눈에 보게 취합해줘', "
            "'짬짬반장이랑 조회수 위주로 묶으면 어때?'처럼 말씀하시면 분석해서 취합·요약해 드려요. "
            "예시를 보며 구성을 대화로 다듬은 뒤 최종 알마인드 파일 한 개로 받으실 수 있어요. 원문의 링크·수치는 그대로 지키고, 자료를 추가하면 이어서 반영해요."
        )
        render_message("assistant", greeting, animate=st.session_state.get('report_greet_stream_pending', False))
        st.session_state.report_greet_stream_pending = False

    last_idx = len(st.session_state.report_messages) - 1
    last_draft_idx = next((i for i in range(last_idx, -1, -1) if st.session_state.report_messages[i].get("draft")), -1)
    for i, turn in enumerate(st.session_state.report_messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('report_stream_next'))
        shown, example_tree = turn["text"], False
        if _has_tree(turn):
            # 🌟 긴 트리 글은 말풍선에 늘어놓지 않는다(스크롤 피로) - 구조는 아래 '현재 알마인드' 그림으로, 말풍선에는 핵심 요약·메모 같은 짧은 글만
            shown = strip_tree_lines(turn["text"]) or _tree_done_sentence(turn)
            example_tree = not (turn.get("draft") or turn.get("report_meta"))   # 의논 중 보여준 예시 트리는 그 자리에서 그림으로
        render_message(turn["role"], shown, animate=should_animate)
        if should_animate:
            st.session_state.report_stream_next = False
        if example_tree:
            _render_preview(turn["text"])
        render_chart_card(turn.get("chart"), f"report_chart_{i}")

        if turn.get("missing"):  # 대조에서 다시 정리해도 남은 문제 - 숨기지 않고 알린다
            if turn.get("missing_kind") == "취합안":
                st.warning("취합안에서 확인이 필요한 항목이에요(SO 보고서에 없는 링크·숫자, 이전 안에서 빠진 값, 요청과 다른 구성). 최종본에 넣기 전에 확인해 주세요: "
                           + ", ".join(turn["missing"][:12]), icon=":material/warning:")
            else:
                st.warning("원문에 있는 다음 링크·숫자·문장이 정리 결과에서 확인되지 않아요. 원문과 대조해 주세요: "
                           + ", ".join(turn["missing"][:12]), icon=":material/warning:")
        if turn.get("checks"):
            with st.expander("원문 대조 기록", icon=":material/verified_user:"):
                for c in turn["checks"]:
                    st.caption(f"· {c}")
        if i == last_draft_idx:
            st.caption(":material/account_tree: 취합안의 구조는 아래 '현재 알마인드'에서 볼 수 있어요. 고칠 부분은 대화로 말씀해 주세요.")

    _render_merge_panel(st.session_state.report_messages)

    conv_id = st.session_state.report_current_conversation_id
    pending = st.session_state.pop('report_pending', None)
    if pending:
        # 🌟 답변 만들기는 백그라운드로 돈다 - 끝나기 전에 다른 탭·대화로 넘어가도 끊기지 않고 대화에 저장된다(ui/job_runner.py)
        earlier = list(st.session_state.report_messages[:-1])
        excluded_so, excluded_materials = _excluded_so(earlier), _excluded_materials(earlier)

        def work(job):
            attachments, failed = [], []
            if pending['files']:
                job.progress = "첨부 파일을 읽는 중... (사진·PDF는 글자를 읽느라 조금 걸려요)"
                attachments, failed = read_attachments(pending['files'])
            notice = [{"role": "assistant", "text": "읽지 못한 첨부 파일이 있어요:\n" + "\n".join(f"· {f}" for f in failed)}] if failed else []
            if not pending['text'] and not attachments:  # 파일만 올렸는데 하나도 못 읽었으면 AI를 부를 것이 없다
                return earlier + [{"role": "user", "text": pending['display']}] + notice
            job.progress = "보고서를 정리하는 중... (원문과 대조하는 중이라 조금 걸려요)"
            new_messages, _ = process_report_turn(
                earlier, pending['text'], profile_df, db_audience, db_content,
                excluded_so=excluded_so, excluded_materials=excluded_materials,
                attachments=attachments, display_text=pending['display'],
            )
            return new_messages + notice

        job_runner.start(conv_id, work, persist=lambda msgs: save_simple_conversation(conv_id, 'report', msgs), label="보고서를 정리하는 중...")

    outcome = job_runner.wait_for(conv_id)
    if outcome:
        status, value = outcome
        if status == "ok":
            st.session_state.report_messages = value
        else:
            st.session_state.report_messages = st.session_state.report_messages + [
                {"role": "assistant", "text": f"⚠️ 보고서를 정리하는 중 오류가 생겼어요. 다시 시도해 주세요. ({type(value).__name__})"}]
        st.session_state.report_stream_next = True
        autosave_simple('report')
        st.rerun()

    submitted = st.chat_input("SO 활동 보고서를 붙여넣거나 파일(알마인드·엑셀·워드·한글·사진 등)을 첨부해 주세요", accept_file="multiple", file_type=FILE_TYPES)
    if submitted:
        text = (submitted.text or "").strip()
        files = [(f.name, f.getvalue()) for f in submitted.files]
        if text or files:
            display = "\n".join(x for x in (text, "[첨부] " + " · ".join(n for n, _ in files) if files else "") if x)
            st.session_state.report_messages.append({"role": "user", "text": display})
            st.session_state.report_pending = {"text": text, "display": display, "files": files}
            st.rerun()
