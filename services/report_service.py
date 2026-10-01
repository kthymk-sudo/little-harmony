# services/report_service.py
# ============================================================
# 📝 보고서 탭 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
#
# 🌟 [활동 보고서 정리] 붙여넣은 SO별 활동 보고서를 알마인드 트리로 정리한다(prompts/report_prompt.py).
#  - 원문 대조(누락 검사): 정리 결과에 원문의 링크와 숫자가 빠짐없이 들어 있는지 시스템이 직접 확인한다.
#    빠졌으면 AI에게 그 항목을 짚어 한 번 다시 정리시키고, 그래도 빠지면 답변에 경고를 남긴다.
#  - SO별 취합: 트리 답변에는 어느 SO·기간의 보고서인지(report_meta)를 같이 저장해서, 여러 SO를
#    한 대화에 붙여넣은 뒤 취합본 알마인드를 만들 수 있게 한다(collect_so_reports).
#  - 시청 데이터 집계를 요청하면 📊 분석 탭의 옛 피벗 엔진(run_pivot_analysis)이 실제 숫자를 계산해 덧붙인다.
# ============================================================
import re

from ai_engine.gemini_api import (
    generate_report_reply, generate_report_free, generate_pivot_insight_reply, is_api_error, read_file_text,
)
from services.code_analyst import build_tables, run_analyst_turn, LIGHT_NOTE
from services.intent_gate import assess, with_notice, last_assistant_text
from utils.file_reader import read_attachment, AttachmentError
from config import SO_REGIONS, REGION_ORDER as _REGION_ORDER
from database.db_manager import summarize_profile_context
from services.analysis_service import run_pivot_analysis, insight_spec_str, insight_rows_str, data_period_str
from utils.emm_export import parse_report_tree
from utils.response_parser import parse_target_conditions

_LONG_PASTE_CHARS = 200          # 이 길이 이상 붙여넣은 글은 "정리할 보고서 원문"으로 보고 누락을 검사한다
_URL = re.compile(r"https?://[^\s)\]>\"'）]+")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MAX_MISSING_SHOWN = 12
_CONTENT_CHANGE = re.compile(r"빼|삭제|제외|없애|지워|줄여|요약|위주|만 |만$|말고|새로|처음부터")  # 내용을 줄이거나 바꾸라는 요청이면 이전 안과 대조하지 않는다


def _tree_part(text):
    """취합안 답변에서 트리 부분만(뒤에 붙는 '구성 메모' 문단은 AI가 쓴 설명이라 대조에서 뺀다)."""
    return text.split("\n구성 메모")[0]


_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_INSTRUCTION_LINE = re.compile(r"정리해|붙여넣|보고서(?:야|예요|에요|입니다|이야)|^\s*\[첨부 파일:")  # 원문이 아니라 실무자가 덧붙인 요청 줄
_MAX_LINE_SHOWN = 40


def find_missing_items(source_text, tree_text, lines=False):
    """원문의 링크와 숫자(두 자리 이상) 중 정리 결과에 없는 것. 링크는 끝의 문장부호 차이를 무시한다.
    lines=True면 숫자·링크 없는 문장이 통째로 빠진 것도 찾는다: 원문 한 줄의 단어(2자 이상) 중 30%를 넘게
    결과 어디에도 없으면 '문장: ...'으로 돌려준다(정리는 문장을 쪼개 옮길 수 있어 줄 단위 일치는 보지 않는다)."""
    compact = re.sub(r"(?<=\d),(?=\d)", "", tree_text)
    missing = []
    for url in dict.fromkeys(u.rstrip(".,;:!?") for u in _URL.findall(source_text)):
        if url not in tree_text:
            missing.append(url)
    body = _URL.sub(" ", source_text)  # 링크 속 숫자는 링크 검사로 충분하다
    for token in dict.fromkeys(t.replace(",", "") for t in _NUMBER.findall(body)):
        if len(token.replace(".", "")) >= 2 and token not in compact:
            missing.append(token)
    if lines:
        have = set(_WORD.findall(tree_text))
        for line in body.splitlines():
            if _INSTRUCTION_LINE.search(line):
                continue
            words = _WORD.findall(line)
            if words and sum(w not in have for w in words) / len(words) > 0.3:
                missing.append("문장: " + line.strip()[:_MAX_LINE_SHOWN])
    return missing


def _compact_history(messages):
    """AI에게 보낼 이전 대화. SO 보고서는 재료로 따로 한 벌 전달하므로, 지나간 긴 원문·SO 정리 트리·옛 취합안은 한 줄로
    줄인다(가장 최근 취합안은 수정 요청 때문에 남긴다). 안 줄이면 같은 보고서가 세 번씩 들어가 느려지고 헷갈린다."""
    last_draft = latest_draft(messages)
    out = []
    for m in messages:
        text = m.get('text', '')
        if m.get('role') == 'user' and len(text) >= _LONG_PASTE_CHARS:
            text = text[:40].replace("\n", " ") + f"… (보고서 원문 {len(text)}자 - 정리본은 위 SO별 보고서 참고)"
        elif m.get('report_meta'):
            text = f"(정리 완료: {m['report_meta'].get('SO') or 'SO 미확인'} 보고서 - 위 SO별 보고서 참고)"
        elif m.get('draft') and m is not last_draft:
            text = "(이전 취합안 - 생략)"
        out.append({**m, 'text': text})
    return out


def _ask_again(history_with_user, first_reply, correction, so_reports_str="", draft_str=""):
    return generate_report_reply(
        history_with_user + [{"role": "assistant", "text": first_reply}, {"role": "user", "text": correction}], "", so_reports_str, draft_str,
    )


def read_attachments(files):
    """올린 파일 [(이름, 바이트)]를 읽는다. 반환: ([{'제목', '본문'}], [읽지 못한 파일의 사유])."""
    ok, failed = [], []
    for name, data in files:
        try:
            ok.append(read_attachment(name, data, ocr=read_file_text))
        except AttachmentError as e:
            failed.append(str(e))
    return ok, failed


def build_material(question, turn, max_rows=60):
    """분석 탭 답변(turn)을 보고서 재료로 만든다: 질문 + 답변 글 + 결과표(앞 max_rows행). 숫자는 분석에서 계산된 그대로다."""
    rows = turn.get('table') or []
    lines = []
    if rows:
        cols = list(rows[0])
        lines = [" | ".join(cols)] + [" | ".join(str(r.get(c, '')) for c in cols) for r in rows[:max_rows]]
        if len(rows) > max_rows:
            lines.append(f"(표 전체 {len(rows)}행 중 {max_rows}행)")
    return {"제목": question.strip()[:40], "본문": turn.get('text', '') + ("\n[표]\n" + "\n".join(lines) if lines else "")}


def collect_materials(messages, excluded=()):
    """대화에 담아 둔 분석 자료들. excluded는 취합에서 뺀 자료의 메시지 번호."""
    return [{**m['material'], 'index': i} for i, m in enumerate(messages)
            if m.get('material') and i not in set(excluded)]


def so_reports_context(reports, materials=()):
    """AI에게 재료로 주는 SO별 정리본 전문과 분석 자료(제외된 것은 이미 빠진 목록을 받는다)."""
    text = "\n\n".join(f"[{r['SO']} · {r['기간'] or '기간 미확인'}]\n{r['text']}" for r in reports)
    for kind, header in (("분석", "[분석 자료 - 분석 탭에서 시청 데이터를 직접 계산한 결과]"), ("첨부", "[첨부 자료 - 실무자가 올린 파일에서 읽은 내용]")):
        group = [m for m in materials if m.get('종류', '분석') == kind]
        if group:
            text += f"\n\n{header}\n" + "\n\n".join(f"<{m['제목']}>\n{m['본문']}" for m in group)
    return text


_SO_FIRST = re.compile(r"SO[를을가이는은]?\s*.{0,8}(가장 큰|제일 큰|최상위|맨 앞|가장 앞|먼저|첫|1단계|큰 가지|큰 주제|상위)"
                       r"|(가장 큰|최상위|맨 앞|큰 가지|큰 주제)\s*.{0,8}SO")   # "SO가 가장 큰 가지", "SO 먼저", "SO를 최상위로"


def _so_first(tree_text, so_names, need=1):
    """취합안의 1단계 가지가 SO 이름 중심인지: 올린 SO(need개)가 모두 1단계 가지로 있거나, 1단계의 80% 이상이 SO 이름이다.
    (SO 가지 옆에 '시청 데이터 분석' 같은 자료 가지가 하나쯤 더 있는 것은 괜찮다)"""
    firsts = [t for d, t in parse_report_tree(tree_text) if d == 1]
    hits = sum(any(n in t for n in so_names) for t in firsts)
    return bool(firsts) and (hits >= need or hits >= 0.8 * len(firsts))


def _last_tree_is_draft(messages):
    """대화에서 가장 최근에 만들어진 트리가 취합안인지(그렇다면 짧은 구성 변경 요청은 그 취합안을 고치는 일이다)."""
    for m in reversed(messages):
        if m.get('draft') or m.get('report_meta'):
            return bool(m.get('draft'))
    return False


def _reply_type(info, reply_text, user_text, draft_context=False):
    """AI가 밝힌 답변유형(정리/의논/취합안). 빠졌으면 트리가 있는 답변은 SO 보고서 정리로 본다(긴 원문을 붙여넣었을 때도).
    draft_context: 가장 최근 트리가 취합안이면 짧은 요청에서 나온 트리는 SO 보고서 정리가 아니라 그 취합안의 수정으로 본다."""
    kind = str(info.get('답변유형') or '').strip() if isinstance(info, dict) else ''
    if kind in ('의논', '취합안', '자유'):
        return kind
    tree = parse_report_tree(reply_text)
    if kind != '정리':
        kind = '정리' if tree and (isinstance(info, dict) or len(user_text) >= _LONG_PASTE_CHARS) else '의논'
    # SO 보고서 정리는 긴 원문을 붙여넣는 일이다. 짧은 요청에 SO 없이 트리가 나왔다면 AI가 유형을 잘못 밝힌 취합안이다
    # (그대로 두면 "SO 미확인" 보고서로 쌓인다).
    if kind == '정리' and tree and len(user_text) < _LONG_PASTE_CHARS and (draft_context or not (isinstance(info, dict) and info.get('SO'))):
        return '취합안'
    return kind


def process_report_turn(messages, user_text, profile_df, db_audience=None, db_content=None, excluded_so=(), excluded_materials=(),
                        attachments=(), display_text=None):
    """반환: (new_messages, chart_spec). chart_spec은 데이터 요청이 없었거나 계산 결과가 비었으면 None.
    excluded_so / excluded_materials: 취합에서 뺀 SO 이름들 / 분석 자료의 메시지 번호 - AI 재료에서도 뺀다.
    attachments: 이번에 올린 파일들 [{'제목', '본문'}](utils/file_reader) - AI에게는 붙여넣은 글처럼 "[첨부 파일: 이름]" 블록으로 전달한다.
    display_text: 화면·저장용 사용자 말풍선 글(첨부 본문을 그대로 저장하지 않으려고). 없으면 user_text."""
    attachments = list(attachments)
    ai_text = (user_text + "".join(f"\n\n[첨부 파일: {a['제목']}]\n{a['본문']}" for a in attachments)).strip()
    user_turn = {"role": "user", "text": user_text if display_text is None else display_text}
    history_with_user = _compact_history(messages) + [{"role": "user", "text": ai_text}]
    profile_context_str = f"{summarize_profile_context(profile_df)}\n{data_period_str(db_audience)}"
    reports = [r for r in collect_so_reports(messages) if r['SO'] not in set(excluded_so)]
    materials = collect_materials(messages, excluded_materials)
    so_str = so_reports_context(reports, materials)

    prev = latest_draft(messages)
    draft_str = _tree_part(prev['text']) if prev else ""   # 구성 변경 요청은 이 취합안을 고치는 것이라 AI에게 따로 보여 준다

    state_line = (f"SO 보고서 {len(reports)}개, 분석·첨부 자료 {len(materials)}개, 취합안 {'있음' if prev else '없음'}, "
                  f"이번에 첨부한 파일 {', '.join(a['제목'] for a in attachments) or '없음'}, 메시지 길이 {len(ai_text)}자")
    # 🌟 먼저 질문의 뜻·의도를 판단한다: 뜻이 불분명하면 되묻고, 잡담·기능 밖 질문은 안내하고, 불법 요청은 거절한다(services/intent_gate.py)
    verdict = assess('report', user_text, last_assistant_text(messages), state_line)
    if verdict['판단'] != '진행':
        return messages + [user_turn, {"role": "assistant", "text": verdict['답변']}], None
    if verdict['작업'] == '분석':
        # 🌟 보고서에 넣을 적당한 데이터 분석(그래프 없이 수치 위주): 분석 AI가 실제 데이터를 계산하고, 그 결과를 분석 자료로 담아 둔다
        # (이후 "취합안에 넣어줘"로 보고서 내용에 추가·변경할 수 있다)
        try:
            reply = run_analyst_turn([], user_text + LIGHT_NOTE, build_tables(db_audience, db_content, profile_df), gate=False)
        except Exception:
            reply = {'text': "⚠️ 데이터를 계산하는 중 문제가 생겼어요. 잠시 뒤 다시 물어봐 주세요."}
        if is_api_error(reply['text']) or reply['text'].startswith("⚠️"):
            return messages + [user_turn, {"role": "assistant", "text": reply['text']}], None
        note = {"role": "assistant", "material": {**build_material(user_text, reply), "종류": "분석"},
                "text": f"{with_notice(reply['text'], verdict)}\n\n(이 분석 결과는 보고서 자료로 담아 뒀어요. '취합안에 넣어줘'라고 하면 보고서 내용에 반영해요.)"}
        return messages + [user_turn, note], None
    if verdict['작업'] == '자유':
        # 🌟 트리 규칙이 없는 가벼운 프롬프트로 답한다(피드백·메일 초안·비교 등 알마인드 밖의 모든 일)
        ai_raw = generate_report_free(history_with_user, so_str, draft_str)
        reply_text, parsed, info, kind = ai_raw.strip(), {}, None, '자유'
    else:
        ai_raw = generate_report_reply(history_with_user, profile_context_str, so_str, draft_str)
        reply_text, parsed = parse_target_conditions(ai_raw)
        parsed = parsed or {}
        info = parsed.get('보고서정보')
        kind = _reply_type(info, reply_text, ai_text, draft_context=_last_tree_is_draft(messages) and not attachments)
    checks = []
    missing = []

    # 🌟 대조 검사: 정리 = 원문의 링크·숫자·문장이 결과에 있는지(긴 원문을 붙여넣었고 트리가 나왔을 때),
    # 취합안 = 결과의 링크·숫자가 SO별 보고서에 실제로 있는지(지어낸 값이 없는지). 문제가 있으면 한 번만 다시 시킨다.
    verify = None
    if kind == '정리' and len(ai_text) >= _LONG_PASTE_CHARS and parse_report_tree(reply_text):
        verify = (lambda t: find_missing_items(ai_text, t, lines=True),
                  "누락 의심 {n}건 → 다시 정리 요청: {items}",
                  "원문에 있는데 정리 결과에서 빠졌거나 바뀐 링크·숫자·문장이 있어: {items}. 원문 그대로 해당 항목에 넣어서 전체 트리를 처음부터 다시 정리해줘. 다른 내용은 바꾸지 마.")
    elif kind == '취합안' and parse_report_tree(reply_text):
        source = "\n".join([r['text'] for r in reports] + [m['본문'] for m in materials] + [a['본문'] for a in attachments])
        # 결과의 링크·숫자는 SO 보고서(와 분석 자료)에 있어야 하고, 구성만 바꾸는 요청이면 이전 취합안의 링크·숫자도 그대로여야 한다
        prev_tree = draft_str if not _CONTENT_CHANGE.search(user_text) else ""
        so_names = [r['SO'] for r in reports] + list(SO_REGIONS)
        wants_so_first = bool(_SO_FIRST.search(user_text))   # "SO가 가장 큰 가지"를 요청했는데 1단계가 SO 이름이 아니면 다시 시킨다
        verify = (lambda t: list(dict.fromkeys(
                      find_missing_items(_tree_part(t), source) + (find_missing_items(prev_tree, _tree_part(t)) if prev_tree else [])
                      + (["구성: 1단계 가지가 SO 이름이 아님"] if wants_so_first and not _so_first(_tree_part(t), so_names, len(reports)) else []))),
                  "취합안 대조 {n}건 → 다시 작성 요청: {items}",
                  "취합안에 문제가 있어: {items}. 링크·숫자는 SO별 보고서(와 분석 자료)에 있는 값만 그대로 써. 구성만 바꾸는 요청이면 "
                  "이전 취합안에 있던 링크·숫자는 빠뜨리지 마. '구성'이 문제라면 실무자가 요청한 대로 1단계 가지(▸)가 SO 이름(대전, 광주 …)이고 "
                  "주요내용은 그 아래 2단계(·)가 되게 바꿔. 전체 취합안을 [현재 취합안]의 내용 그대로 처음부터 다시 작성해줘.")
    if verify:
        check, note, fix = verify
        missing = check(reply_text)
        if missing:
            items = ", ".join(missing[:_MAX_MISSING_SHOWN])
            checks.append(note.format(n=len(missing), items=items))
            retry_raw = _ask_again(history_with_user, reply_text, fix.format(items=items), so_str, draft_str)
            if not is_api_error(retry_raw):
                retry_text, retry_parsed = parse_target_conditions(retry_raw)
                retry_missing = check(retry_text) if parse_report_tree(retry_text) else missing
                if len(retry_missing) <= len(missing):  # 더 나아졌을 때만 바꾼다
                    reply_text, parsed, missing = retry_text, retry_parsed or parsed, retry_missing
                    info = parsed.get('보고서정보')

    if kind == '자유':
        # 🌟 자유 작업(피드백·메일 초안·비교 등): 답에 나온 링크·숫자가 올린 자료나 요청에 없으면 알린다. 합계·증감처럼 AI가 계산한 값일 수도 있어
        # 다시 시키지는 않고 경고만 한다.
        evidence = "\n".join([user_text, ai_text, draft_str] + [r['text'] for r in reports] + [m['본문'] for m in materials])
        missing = find_missing_items(reply_text, evidence)

    data_spec = parsed.get('데이터요청') or {}
    chart_spec = None

    if data_spec.get('행') and data_spec.get('측정값'):
        pivot_result = run_pivot_analysis(db_audience, profile_df, data_spec, db_content)
        if pivot_result and pivot_result.get('결과'):
            chart_spec = {"type": "pivot", "data": pivot_result}
            data_insight = generate_pivot_insight_reply(
                user_text, insight_spec_str(data_spec, pivot_result), insight_rows_str(pivot_result),
            )
            if not is_api_error(data_insight):
                reply_text = f"{reply_text}\n\n📊 데이터 인사이트\n{data_insight}"
            else:
                reply_text = f"{reply_text}\n\n📊 데이터는 계산했지만 설명 생성에는 실패했어요. 아래 표/차트를 참고해주세요."

    new_message = {"role": "assistant", "text": with_notice(reply_text, verdict)}
    has_tree = bool(parse_report_tree(reply_text))
    if kind == '정리' and isinstance(info, dict) and has_tree:
        new_message["report_meta"] = {"SO": str(info.get('SO') or '').strip(), "기간": str(info.get('기간') or '').strip()}
    elif kind == '취합안' and has_tree:
        new_message["draft"] = True  # 최종 알마인드로 내려받을 수 있는 취합안 (의논 중 예시는 여기에 해당하지 않는다)
    if missing:  # 다시 정리해도 남은 문제는 숨기지 않고 알린다
        new_message["missing"] = missing[:_MAX_MISSING_SHOWN * 2]
        if kind in ('취합안', '자유'):
            new_message["missing_kind"] = kind
    if checks:
        new_message["checks"] = checks
    if chart_spec:
        new_message["chart"] = chart_spec
    # 정리(SO 보고서 자체)가 아니면 첨부 파일은 이후 대화에서도 쓸 수 있게 자료로 담아 둔다(답변보다 앞에 두어 이 답변이 낡은 취합안으로 보이지 않게)
    notes = [] if kind == '정리' else [
        {"role": "assistant", "text": f"첨부 자료를 담았어요: {a['제목']}", "material": {**a, "종류": "첨부"}} for a in attachments]
    new_messages = messages + [user_turn] + notes + [new_message]
    return new_messages, chart_spec


def _so_sort_key(so_name):
    """취합본의 SO 순서: 대전 → 충청 → 세종 → 광주 → 전남 → 영등포 → 동대문 → 대구(config.SO_REGIONS 순서), 그 밖은 뒤에."""
    for i, region in enumerate(_REGION_ORDER):
        if region in so_name:
            return (i, so_name)
    return (len(_REGION_ORDER), so_name)


def _region_aliases():
    """권역별 부르는 이름: 권역 이름 + 회사명에서 뗀 세부 이름(대전→동대전, 광주→광주동부, 대구→수성)."""
    aliases = {}
    for region, companies in SO_REGIONS.items():
        names = {region}
        for company in companies:
            short = company.replace("㈜", "").replace("씨엠비", "").replace("방송", "").strip()
            if short:
                names.add(short)
        aliases[region] = names
    return aliases


_ALIASES = _region_aliases()


def region_of(so_name):
    """SO 이름이 속한 기본 권역(동대전→대전, 광주동부→광주, 수성→대구). 모르면 None. 보고서는 권역 단위로 취합한다."""
    best = None
    for region, names in _ALIASES.items():
        for name in names:
            if name in (so_name or "") and (best is None or len(name) > best[0]):
                best = (len(name), region)
    return best[1] if best else None


def collect_so_reports(messages):
    """대화에서 정리된 SO별 보고서 트리를 모은다(권역 단위 - 세부 SO 이름은 권역으로 묶는다). 같은 권역을 다시
    붙여넣었으면 나중 것이 우선이고 '교체' 횟수를 남긴다. 권역을 모르는 SO는 이름(표기 차이 무시)으로, 이름조차
    모르면 'SO 미확인 N'으로 따로 둬서 서로 덮어쓰지 않게 한다.
    반환: [{'SO', '원본이름', '기간', 'nodes', 'index', 'text', '교체'}] (권역 순서대로)."""
    latest, unknown = {}, 0
    for i, m in enumerate(messages):
        meta = m.get('report_meta')
        if m.get('role') != 'assistant' or not meta:
            continue
        nodes = parse_report_tree(m.get('text', ''))
        if not nodes:
            continue
        raw = (meta.get('SO') or '').strip()
        region = region_of(raw)
        if region:
            key = name = region
        elif raw:
            key, name = re.sub(r"\s|SO|so|지사", "", raw) or raw, raw
        else:
            unknown += 1
            key = name = f"SO 미확인 {unknown}"
        latest[key] = {'SO': name, '원본이름': raw or name, '기간': meta.get('기간', ''), 'nodes': nodes, 'index': i,
                       'text': m['text'], '교체': latest[key]['교체'] + 1 if key in latest else 0}
    return sorted(latest.values(), key=lambda r: _so_sort_key(r['SO']))


def latest_draft(messages):
    """가장 최근 취합안 답변(없으면 None)."""
    return next((m for m in reversed(messages) if m.get('draft') and m.get('role') == 'assistant'), None)


def draft_is_stale(messages):
    """취합안을 만든 뒤에 SO 보고서가 새로 정리되거나 교체됐으면 True(그 SO는 취합안에 반영되지 않았다)."""
    draft = latest_draft(messages)
    if draft is None:
        return False
    after = messages[next(i for i, m in enumerate(messages) if m is draft) + 1:]
    return any(m.get('report_meta') or m.get('material') for m in after)


def merged_title(reports):
    """취합본 중심토픽 제목: 모든 SO의 기간이 같으면 그 기간 + 'SO별 활동 보고 취합', 다르면(표기가 SO마다 다를 수 있다) 기간 없이."""
    periods = {r['기간'] for r in reports if r['기간']}
    return f"{periods.pop() if len(periods) == 1 else ''} SO별 활동 보고 취합".strip()


if __name__ == "__main__":
    source = ("● 9월 4주차\n1. 캠페인 진행 https://rainbowtv.app.link/tbVHa8ZYs6b 콘텐츠 조회수 14회에서 70회로 400% 증가, "
              "총 13개소 참여, 사진 125건, 소식지 https://blog.naver.com/cmb_rainbowtv/224419534332.\n" + "13개소 " * 60)
    good = ("📌 대전 · 9월 4주차\n  ▸ 캠페인 진행\n    · https://rainbowtv.app.link/tbVHa8ZYs6b\n    · 콘텐츠 조회수 14회에서 70회로 400% 증가\n"
            "    · 총 13개소 참여, 사진 125건, 소식지\n    · https://blog.naver.com/cmb_rainbowtv/224419534332")
    assert find_missing_items(source, good, lines=True) == [], find_missing_items(source, good, lines=True)
    assert find_missing_items(source, good.replace("소식지", "").replace("콘텐츠 조회수 14회에서 70회로 400% 증가", "70회 400"), lines=True)[-1].startswith("문장: 1. 캠페인 진행")
    bad = good.replace("400%", "4배").replace("224419534332", "999").replace("125건", "많은 건")
    assert find_missing_items(source, bad) == ['https://blog.naver.com/cmb_rainbowtv/224419534332', '400', '125'], find_missing_items(source, bad)

    # 흐름: 첫 정리에서 링크가 빠지면 다시 요청하고, 나아지면 그 결과를 쓴다. 대화에 SO/기간을 남긴다.
    calls = []
    replies = iter([bad + '\n```json\n{"데이터요청": {}, "보고서정보": {"SO": "대전", "기간": "9월 4주차"}}\n```',
                    good + '\n```json\n{"데이터요청": {}, "보고서정보": {"SO": "대전", "기간": "9월 4주차"}}\n```'])
    globals()['generate_report_reply'] = lambda history, ctx, so='', draft='': calls.append((history, so, draft)) or next(replies)
    import services.intent_gate as _gate
    routes = []   # 요청 판단: 따로 정하지 않으면 진행 + 트리 작업
    _gate._ask = lambda prompt: routes.pop(0) if routes else '{"판단": "진행", "작업": "트리"}'
    free_calls = []
    globals()['generate_report_free'] = lambda history, so='', draft='': free_calls.append((history, so, draft)) or "광주는 신규 가입 412명이에요. 합계 5313(계산)."
    globals()['summarize_profile_context'] = lambda p: ""
    msgs, chart = process_report_turn([], source, None, None, None)
    reply = msgs[-1]
    assert len(calls) == 2 and "빠졌거나 바뀐" in calls[1][0][-1]['text'] and reply['text'].startswith("📌 대전") and 'missing' not in reply
    assert reply['report_meta'] == {"SO": "대전", "기간": "9월 4주차"} and reply['checks'], reply
    # 다시 정리해도 빠지면 숨기지 않고 남긴다 / 짧은 글(질문·수정 요청)은 검사하지 않는다
    replies = iter([bad + '\n```json\n{"보고서정보": {"SO": "대전"}}\n```'] * 2)
    assert 'missing' in process_report_turn([], source, None, None, None)[0][-1]
    calls.clear(); replies = iter([good + '\n```json\n{"보고서정보": {"SO": "대전"}}\n```'])
    process_report_turn([], "링크만 모아줘", None, None, None)
    assert len(calls) == 1

    # SO별 취합: 같은 SO는 나중 것이 우선, SO 순서와 제목
    def _msg(so, period, extra=""):
        return {"role": "assistant", "text": f"📌 {so} · {period} 활동 보고\n  ▸ 실적{extra}", "report_meta": {"SO": so, "기간": period}}
    reports = collect_so_reports([_msg("광주", "9월 4주차"), {"role": "user", "text": "x"}, _msg("대전", "9월 4주차"), _msg("광주", "9월 4주차", "\n    · 수정본")])
    assert [r['SO'] for r in reports] == ["대전", "광주"] and len(reports[1]['nodes']) == 3 and merged_title(reports) == "9월 4주차 SO별 활동 보고 취합" and merged_title(reports + [{"기간": "9월 3주차"}]) == "SO별 활동 보고 취합"
    # 권역 단위: 광주 SO/광주동부→광주, 동대전→대전(같은 권역은 나중 것이 교체), 수성→대구. 이름을 모르는 보고서끼리는 덮어쓰지 않는다
    reports = collect_so_reports([_msg("광주 SO", "9월 4주차"), _msg("동대전", "9월 4주차"), _msg("본사", "9월 4주차"), _msg("광주동부", "9월 4주차"),
                                  _msg("", "9월 4주차", "\n  ▸ A"), _msg("", "9월 4주차", "\n  ▸ B"), _msg("수성", "9월 4주차")])
    assert [r['SO'] for r in reports] == ["대전", "광주", "대구", "SO 미확인 1", "SO 미확인 2", "본사"], [r['SO'] for r in reports]
    assert [r['교체'] for r in reports][:2] == [0, 1] and reports[1]['원본이름'] == "광주동부"
    assert region_of("㈜씨엠비동대전방송") == "대전" and region_of("영등포 SO") == "영등포" and region_of("본사") is None

    # 이전 대화 줄이기: 긴 원문·SO 정리 트리·옛 취합안은 한 줄로, 마지막 취합안은 그대로
    d1, d2 = {"role": "assistant", "text": "📌 옛 취합안", "draft": True}, {"role": "assistant", "text": "📌 새 취합안", "draft": True}
    hist = [{"role": "user", "text": "원문 " * 200}, {"role": "assistant", "text": good, "report_meta": {"SO": "대전"}}, d1, d2]
    compact = _compact_history(hist)
    assert "보고서 원문" in compact[0]['text'] and "정리 완료: 대전" in compact[1]['text'] and "생략" in compact[2]['text'] and compact[3]['text'] == "📌 새 취합안"
    assert not draft_is_stale(hist) and draft_is_stale(hist + [_msg("충청", "9월")]) and not draft_is_stale([_msg("충청", "9월")])

    # 의논/취합안: SO 보고서 전문이 재료로 전달되고, 취합안은 SO 보고서에 없는 값을 거꾸로 대조하며, 의논 예시는 최종본(draft)이 되지 않는다
    prior = [{"role": "user", "text": "대전 정리"}, {"role": "assistant", "text": good, "report_meta": {"SO": "대전", "기간": "9월 4주차"}}]
    calls.clear()
    fence = "\n```json\n{}\n```"
    replies = iter(["조회수를 보고한 SO는 대전뿐이에요. 예시:\n📌 예시\n  ▸ 조회수\n    · 대전 : 70회" + fence.format('{"보고서정보": {"답변유형": "의논"}}')])
    m = process_report_turn(prior, "조회수 위주로 묶으면?", None, None, None)[0][-1]
    assert "https://rainbowtv.app.link/tbVHa8ZYs6b" in calls[0][1] and 'draft' not in m and 'report_meta' not in m
    tail = fence.replace("{}", '{"보고서정보": {"답변유형": "취합안"}}')
    fake = "📌 취합\n  ▸ 조회수\n    · 대전 : 70회, 999명"
    ok = "📌 취합\n  ▸ 조회수\n    · 대전 : 14회 → 70회 (400% 증가)"
    replies = iter([fake + tail, ok + tail]); calls.clear()
    m = process_report_turn(prior, "조회수 위주로 취합해줘", None, None, None)[0][-1]
    assert m.get('draft') and 'missing' not in m and m['checks'] and "999" in m['checks'][0] and m['text'] == ok, m
    assert latest_draft(prior + [m]) is m and latest_draft(prior) is None
    # 분석 자료: 표는 앞 행만, 자료는 재료로 전달되고, 취합안의 숫자는 자료 본문과도 대조되며, 제외하면 빠진다. 자료가 나중에 담기면 취합안은 낡은 것
    mat = build_material("6월 SO별 MAU", {"text": "대전 688명이 가장 많아요.", "table": [{"SO": "대전", "MAU": 688}, {"SO": "충청", "MAU": 71}]}, max_rows=1)
    assert mat["제목"] == "6월 SO별 MAU" and "SO | MAU\n대전 | 688" in mat["본문"] and "충청" not in mat["본문"] and "2행 중 1행" in mat["본문"], mat
    note = {"role": "assistant", "text": "담았어요", "material": mat}
    with_mat = prior + [note]
    assert [m["index"] for m in collect_materials(with_mat)] == [2] and collect_materials(with_mat, excluded=[2]) == []
    assert "[분석 자료" in so_reports_context([], collect_materials(with_mat)) and "[분석 자료" not in so_reports_context([], [])
    calls.clear(); ok2 = "📌 취합\n  ▸ 시청 데이터\n    · 대전 688명"
    replies = iter([ok2 + tail]); m = process_report_turn(with_mat, "MAU도 넣어서 취합안 만들어줘", None, None, None)[0][-1]
    assert m.get('draft') and 'missing' not in m and "688" in calls[0][1] and "[분석 자료" in calls[0][1], m
    calls.clear(); replies = iter([ok2.replace("688", "999") + tail, ok2.replace("688", "999") + tail])
    assert 'missing' in process_report_turn(with_mat, "MAU도 넣어서 취합안 만들어줘", None, None, None)[0][-1]
    calls.clear(); replies = iter(["📌 취합" + tail])  # 자료를 뺐으니 688은 근거가 없다 - 숫자 없는 답만 받는다
    process_report_turn(with_mat, "MAU도 넣어서 취합안 만들어줘", None, None, None, excluded_materials=[2])
    assert "[분석 자료" not in calls[0][1]
    assert draft_is_stale([{"role": "assistant", "text": "📌 취합", "draft": True}, note]) and not draft_is_stale([note, {"role": "assistant", "text": "📌 취합", "draft": True}])
    # AI가 유형을 "정리"로 잘못 밝혀도 짧은 요청에 SO 없이 나온 트리는 취합안이다(SO 미확인 보고서로 쌓이지 않게). 긴 원문이면 정리
    # 구성 변경: 메모 속 숫자는 대조하지 않고, 구성만 바꾸는 요청에서 이전 취합안의 숫자가 사라지면 다시 시키며, 빼 달라는 요청이면 허용한다
    prev_draft = {"role": "assistant", "text": "📌 취합\n  ▸ 조회수\n    · 대전 : 14회 → 70회\n    · 광주 : 13개소", "draft": True}
    base = prior + [prev_draft]
    memo = "\n\n구성 메모: 조회수를 한 가지로 묶었고 3곳을 정리했어요."
    same = "📌 취합\n  ▸ 광주\n    · 13개소\n  ▸ 대전\n    · 14회 → 70회"
    calls.clear(); replies = iter([same + memo + tail])
    m = process_report_turn(base, "SO 먼저 나누고 그 아래에 주요내용으로 뒤집어줘", None, None, None)[0][-1]
    assert m.get('draft') and 'missing' not in m and 'checks' not in m and "구성 메모" in m['text'], m
    lost = "📌 취합\n  ▸ 대전\n    · 14회 → 70회"
    calls.clear(); replies = iter([lost + tail, same + tail])
    m = process_report_turn(base, "SO 먼저 나누고 그 아래에 주요내용으로 뒤집어줘", None, None, None)[0][-1]
    assert len(calls) == 2 and '13' in calls[1][0][-1]['text'] and 'missing' not in m and "13개소" in m['text'], m
    calls.clear(); replies = iter([lost + tail])
    m = process_report_turn(base, "광주는 빼줘", None, None, None)[0][-1]
    assert len(calls) == 1 and 'missing' not in m

    # 올린 파일 읽기: 읽은 것과 못 읽은 사유를 나눠 돌려준다(사진은 AI가 글자를 읽는다)
    globals()['read_file_text'] = lambda data, mime: "사진 속 글자 70회"
    ok_files, bad_files = read_attachments([("a.txt", "안녕".encode()), ("b.png", b"x"), ("c.exe", b"x")])
    assert [a["제목"] for a in ok_files] == ["a.txt", "b.png"] and ok_files[1]["본문"] == "사진 속 글자 70회" and len(bad_files) == 1 and "c.exe" in bad_files[0]

    # 첨부 파일: AI에는 붙여넣은 글처럼 전달하고 화면/저장에는 짧은 글만, 정리가 아니면 자료로 담아 두며 취합안 대조에도 쓴다
    att = [{"제목": "표.xlsx", "본문": "SO | 시청자\n대전 | 4321\n광주 | 987"}]
    calls.clear(); replies = iter(["📌 취합\n  ▸ 첨부 표\n    · 대전 4321" + tail])
    out = process_report_turn(prior, "이 표도 넣어서 취합안 만들어줘", None, None, None, attachments=att, display_text="이 표도 넣어서 취합안 만들어줘\n[첨부] 표.xlsx")[0]
    assert "[첨부 파일: 표.xlsx]" in calls[0][0][-1]['text'] and "4321" in calls[0][0][-1]['text']
    assert out[-3]['text'].endswith("[첨부] 표.xlsx") and out[-2]['material']['종류'] == "첨부" and out[-1].get('draft') and 'missing' not in out[-1], out
    assert "[첨부 자료" in so_reports_context([], collect_materials(out)) and "[분석 자료" not in so_reports_context([], collect_materials(out))
    assert not draft_is_stale(out)
    src_att = [{"제목": "대전.txt", "본문": "1. 캠페인 진행\n총 13개소 경로당 참여, 사진 125건 https://a.b/c"}]
    calls.clear(); replies = iter([good + '\n```json\n{"보고서정보": {"답변유형": "정리", "SO": "대전"}}\n```'] * 2)
    out = process_report_turn([], "대전 보고서야", None, None, None, attachments=src_att * 8)[0]  # 원문(첨부)이 길면 대조 검사를 한다
    assert out[-1].get('report_meta') and all(m.get('material') is None for m in out), out

    # "SO가 가장 큰 가지": 요청 표현을 알아보고, 1단계가 SO 이름이 아니면 다시 시키며, 현재 취합안을 AI에게 보여 주고, 취합안 뒤의 짧은 요청은 정리가 아니라 수정이다
    assert all(_SO_FIRST.search(t) for t in ("SO가 가장 큰 가지가 되게해줘", "SO를 최상위로", "SO 먼저 나누고 그 아래에 주요내용으로", "가장 큰 가지는 SO로"))
    assert not any(_SO_FIRST.search(t) for t in ("주요내용 기준으로 먼저 나누고 그 아래에 SO별로", "조회수를 맨 위로"))
    assert _so_first("📌 취합\n  ▸ 대전\n    · 조회수\n  ▸ 광주 SO\n    · 조회수", ["대전", "광주"]) and not _so_first("📌 취합\n  ▸ 조회수\n    · 대전", ["대전", "광주"])
    assert _so_first("📌 취합\n  ▸ 대전\n  ▸ 광주\n  ▸ 시청 데이터 분석", ["대전", "광주"], need=2), "SO 가지 옆의 자료 가지는 괜찮다"
    assert not _so_first("📌 취합\n  ▸ 대전\n  ▸ 조회수\n  ▸ 실적", ["대전", "광주"], need=2)
    topic_first = "📌 취합\n  ▸ 조회수\n    · 대전 : 14회 → 70회\n    · 광주 : 13개소"
    so_first = "📌 취합\n  ▸ 대전\n    · 조회수\n      - 14회 → 70회\n  ▸ 광주\n    · 조회수\n      - 13개소"
    base = prior + [{"role": "assistant", "text": topic_first + "\n\n구성 메모: 묶음", "draft": True}]
    calls.clear(); replies = iter([topic_first + tail, so_first + tail])
    m = process_report_turn(base, "SO가 가장 큰 가지가 되게해줘", None, None, None)[0][-1]
    assert len(calls) == 2 and "구성: 1단계" in calls[1][0][-1]['text'] and "14회" in calls[0][2] and "구성 메모" not in calls[0][2], calls
    assert m['draft'] and m['text'].startswith(so_first) and 'missing' not in m, m
    calls.clear(); replies = iter([good + '\n```json\n{"보고서정보": {"답변유형": "정리", "SO": "대전"}}\n```'])   # AI가 취합안 수정을 SO 정리로 잘못 밝혀도
    m = process_report_turn(base, "순서를 바꿔줘", None, None, None)[0][-1]
    assert m.get('draft') and 'report_meta' not in m, m

    # 자유 작업(피드백·메일 초안 등): 트리가 아니라 글이고(글머리 목록이 트리로 오인되지 않는다), 자료에 없는 숫자는 다시 시키지 않고 경고만 한다
    free = "이번 주 피드백이에요.\n- 대전 : 조회수가 14회에서 70회로 늘었어요.\n- 광주 : 신규 가입 999명이에요(자료에 없는 값).\n- 충청 : 보고가 없어요."
    calls.clear(); replies = iter([free + '\n```json\n{"보고서정보": {"답변유형": "자유"}}\n```'])
    m = process_report_turn(base, "이 취합안에 대해 피드백해줘", None, None, None)[0][-1]
    assert len(calls) == 1 and m['text'] == free and m['missing'] == ['999'] and m['missing_kind'] == "자유" and not m.get('draft') and 'report_meta' not in m, m
    calls.clear(); replies = iter([free.replace("999명", "412명") + '\n```json\n{"보고서정보": {"답변유형": "자유"}}\n```'])
    m = process_report_turn(base, "피드백해줘 (신규 가입 412명 기준으로)", None, None, None)[0][-1]
    assert 'missing' not in m, "요청에 쓴 숫자는 근거로 인정한다"

    # 요청 분류: 자유 작업은 트리 프롬프트를 거치지 않고 가벼운 프롬프트로 답하며(자료와 현재 취합안은 전달), 분류가 실패하면 트리 작업으로 본다
    # 요청 판단: 거절·되묻기·안내는 본 작업을 부르지 않고 그 답만 대화에 남기며(첨부는 담지 않는다), 진행의 '주의'는 답 끝에 붙는다
    calls.clear(); free_calls.clear()
    routes.append('{"판단": "거절", "답변": "문서 보안(DRM) 해제는 도와드릴 수 없어요. 보안팀에 반출 절차를 문의해 보시겠어요?"}')
    out = process_report_turn(base, "이 문서 DRM 풀어줘", None, None, None, attachments=att)[0]
    assert not calls and not free_calls and out[-1]['text'].startswith("문서 보안(DRM)") and len(out) == len(base) + 2 and not any(m.get('material') for m in out[len(base):])
    routes.append('{"판단": "되묻기", "답변": "어떤 SO의 어느 주차 내용을 말씀하시나요?"}')
    assert process_report_turn(base, "그거 해줘", None, None, None)[0][-1]['text'].startswith("어떤 SO") and not calls
    routes.append('{"판단": "진행", "작업": "자유", "주의": "개인정보가 들어 있으면 공유 범위를 확인하세요."}')
    out = process_report_turn(base, "메일 써줘", None, None, None)[0][-1]
    assert out['text'].endswith("※ 참고: 개인정보가 들어 있으면 공유 범위를 확인하세요.") and free_calls
    calls.clear(); free_calls.clear(); routes.append('{"판단": "진행", "작업": "자유"}')
    out = process_report_turn(base, "412명이 맞는지 확인해줘", None, None, None)[0]
    assert not calls and len(free_calls) == 1 and "대전" in free_calls[0][1] and "조회수" in free_calls[0][2], (calls, free_calls)
    assert out[-1]['text'].startswith("광주는") and out[-1]['missing'] == ['5313'] and out[-1]['missing_kind'] == "자유" and not out[-1].get('draft'), out[-1]
    calls.clear(); free_calls.clear(); routes.append("⚠️ 서버 오류")
    replies = iter([topic_first + tail])
    assert process_report_turn(base, "조회수 위주로 다시", None, None, None)[0][-1].get('draft') and not free_calls and len(calls) == 1
    routes.append('설명 없는 이상한 응답')
    replies = iter([topic_first + tail])
    assert process_report_turn(base, "조회수 위주로 다시", None, None, None)[0][-1].get('draft') and not free_calls

    # 보고서 안의 적당한 데이터 분석: 분석 AI가 계산하고(그래프 없이 짧게), 그 결과가 분석 자료로 담겨 이후 취합안에 넣을 수 있으며, 취합안 뒤에 담기면 취합안은 낡은 것이다
    asked = []
    globals()['build_tables'] = lambda *a: {}
    globals()['run_analyst_turn'] = lambda msgs, q, tables, **k: asked.append((q, k)) or {'role': 'assistant', 'text': '대전 6월 MAU는 688명이에요.', 'table': [{'SO': '대전', 'MAU': 688}]}
    routes.append('{"판단": "진행", "작업": "분석"}')
    out = process_report_turn(base, "대전 6월 MAU 확인해서 자료에 넣어줘", None, None, None)[0]
    assert asked[0][0].startswith("대전 6월 MAU 확인해서") and "그래프" in asked[0][0] and asked[0][1] == {"gate": False}, asked
    assert out[-1]['text'].startswith("대전 6월 MAU는 688명이에요.") and "자료로 담아 뒀어요" in out[-1]['text'] and not out[-1].get('draft'), out[-1]
    assert out[-1]['material']['종류'] == "분석" and "대전 | 688" in out[-1]['material']['본문'] and [m['제목'] for m in collect_materials(out)] == ["대전 6월 MAU 확인해서 자료에 넣어줘"]
    assert draft_is_stale(out), "취합안을 만든 뒤 분석이 담기면 취합안은 낡은 것"
    globals()['run_analyst_turn'] = lambda msgs, q, tables, **k: {'role': 'assistant', 'text': '⚠️ 분석 중 오류'}
    routes.append('{"판단": "진행", "작업": "분석"}')
    out = process_report_turn(base, "대전 MAU", None, None, None)[0]
    assert out[-1]['text'].startswith("⚠️") and 'material' not in out[-1], "실패한 분석은 자료로 담지 않는다"

    mislabeled ='\n```json\n{"보고서정보": {"답변유형": "정리", "SO": "", "기간": ""}}\n```'
    calls.clear(); replies = iter([ok2 + mislabeled])
    m = process_report_turn(with_mat, "취합안 만들어줘", None, None, None)[0][-1]
    assert m.get('draft') and 'report_meta' not in m, m
    calls.clear(); replies = iter([good + mislabeled])
    assert process_report_turn([], source, None, None, None)[0][-1].get('report_meta') is not None
    print("report_service self-check OK")
