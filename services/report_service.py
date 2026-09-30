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

from ai_engine.gemini_api import generate_report_reply, generate_pivot_insight_reply, is_api_error
from config import SO_REGIONS
from database.db_manager import summarize_profile_context
from services.analysis_service import run_pivot_analysis, insight_spec_str, insight_rows_str, data_period_str
from utils.emm_export import parse_report_tree
from utils.response_parser import parse_target_conditions

_LONG_PASTE_CHARS = 200          # 이 길이 이상 붙여넣은 글은 "정리할 보고서 원문"으로 보고 누락을 검사한다
_URL = re.compile(r"https?://[^\s)\]>\"'）]+")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MAX_MISSING_SHOWN = 12


_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_INSTRUCTION_LINE = re.compile(r"정리해|붙여넣|보고서(?:야|예요|에요|입니다|이야)")  # 원문이 아니라 실무자가 덧붙인 요청 줄
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


def _ask_again(history_with_user, first_reply, correction, so_reports_str=""):
    return generate_report_reply(
        history_with_user + [{"role": "assistant", "text": first_reply}, {"role": "user", "text": correction}], "", so_reports_str,
    )


def so_reports_context(reports):
    """AI에게 재료로 주는 SO별 정리본 전문(제외된 SO는 이미 빠진 목록을 받는다)."""
    return "\n\n".join(f"[{r['SO']} · {r['기간'] or '기간 미확인'}]\n{r['text']}" for r in reports)


def _reply_type(info, reply_text, user_text):
    """AI가 밝힌 답변유형(정리/의논/취합안). 빠졌으면 트리가 있는 답변은 SO 보고서 정리로 본다(긴 원문을 붙여넣었을 때도)."""
    kind = str(info.get('답변유형') or '').strip() if isinstance(info, dict) else ''
    if kind in ('정리', '의논', '취합안'):
        return kind
    tree = parse_report_tree(reply_text)
    return '정리' if tree and (isinstance(info, dict) or len(user_text) >= _LONG_PASTE_CHARS) else '의논'


def process_report_turn(messages, user_text, profile_df, db_audience=None, db_content=None, excluded_so=()):
    """반환: (new_messages, chart_spec). chart_spec은 데이터 요청이 없었거나 계산 결과가 비었으면 None.
    excluded_so: 취합에서 뺀 SO 이름들 - AI 재료에서도 뺀다."""
    user_turn = {"role": "user", "text": user_text}
    history_with_user = _compact_history(messages) + [user_turn]
    profile_context_str = f"{summarize_profile_context(profile_df)}\n{data_period_str(db_audience)}"
    reports = [r for r in collect_so_reports(messages) if r['SO'] not in set(excluded_so)]
    so_str = so_reports_context(reports)

    ai_raw = generate_report_reply(history_with_user, profile_context_str, so_str)
    reply_text, parsed = parse_target_conditions(ai_raw)
    parsed = parsed or {}
    info = parsed.get('보고서정보')
    kind = _reply_type(info, reply_text, user_text)
    checks = []
    missing = []

    if kind == '정리' and len(user_text) >= _LONG_PASTE_CHARS and parse_report_tree(reply_text):
        # 🌟 원문 대조: 긴 보고서 원문을 붙여넣었고 트리가 나왔을 때 - 원문의 링크·숫자가 결과에 있어야 한다
        missing = find_missing_items(user_text, reply_text, lines=True)
        if missing:
            checks.append(f"누락 의심 {len(missing)}건 → 다시 정리 요청: {', '.join(missing[:_MAX_MISSING_SHOWN])}")
            fix = ("원문에 있는데 정리 결과에서 빠졌거나 바뀐 링크·숫자·문장이 있어: " + ", ".join(missing[:_MAX_MISSING_SHOWN]) +
                   ". 원문 그대로 해당 항목에 넣어서 전체 트리를 처음부터 다시 정리해줘. 다른 내용은 바꾸지 마.")
            retry_raw = _ask_again(history_with_user, reply_text, fix, so_str)
            if not is_api_error(retry_raw):
                retry_text, retry_parsed = parse_target_conditions(retry_raw)
                retry_missing = find_missing_items(user_text, retry_text, lines=True) if parse_report_tree(retry_text) else missing
                if len(retry_missing) <= len(missing):  # 더 나아졌을 때만 바꾼다
                    reply_text, parsed, missing = retry_text, retry_parsed or parsed, retry_missing
                    info = parsed.get('보고서정보')
    elif kind == '취합안' and parse_report_tree(reply_text):
        # 🌟 거꾸로 대조: 취합안의 링크·숫자가 SO별 보고서에 실제로 있는지(지어낸 값이 없는지)
        source = "\n".join(r['text'] for r in reports)
        missing = find_missing_items(reply_text, source)
        if missing:
            checks.append(f"SO 보고서에 없는 값 {len(missing)}건 → 다시 작성 요청: {', '.join(missing[:_MAX_MISSING_SHOWN])}")
            fix = ("취합안에 SO별 보고서에 없는 링크·숫자가 있어: " + ", ".join(missing[:_MAX_MISSING_SHOWN]) +
                   ". SO별 보고서에 있는 값만 그대로 써서 전체 취합안을 처음부터 다시 작성해줘.")
            retry_raw = _ask_again(history_with_user, reply_text, fix, so_str)
            if not is_api_error(retry_raw):
                retry_text, retry_parsed = parse_target_conditions(retry_raw)
                retry_missing = find_missing_items(retry_text, source) if parse_report_tree(retry_text) else missing
                if len(retry_missing) <= len(missing):
                    reply_text, parsed, missing = retry_text, retry_parsed or parsed, retry_missing
                    info = parsed.get('보고서정보')

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

    new_message = {"role": "assistant", "text": reply_text}
    has_tree = bool(parse_report_tree(reply_text))
    if kind == '정리' and isinstance(info, dict) and has_tree:
        new_message["report_meta"] = {"SO": str(info.get('SO') or '').strip(), "기간": str(info.get('기간') or '').strip()}
    elif kind == '취합안' and has_tree:
        new_message["draft"] = True  # 최종 알마인드로 내려받을 수 있는 취합안 (의논 중 예시는 여기에 해당하지 않는다)
    if missing:  # 다시 정리해도 남은 문제는 숨기지 않고 알린다
        new_message["missing"] = missing[:_MAX_MISSING_SHOWN * 2]
        if kind == '취합안':
            new_message["missing_kind"] = "취합안"
    if checks:
        new_message["checks"] = checks
    if chart_spec:
        new_message["chart"] = chart_spec
    new_messages = messages + [user_turn, new_message]
    return new_messages, chart_spec


_REGION_ORDER = list(SO_REGIONS)


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


def region_status(reports):
    """기본 권역(config.SO_REGIONS) 8곳별 제출 여부. 반환: ([(권역, 들어온 SO 이름 또는 None)], [권역에 안 맞는 SO 이름])."""
    by_region = {r['SO']: r['SO'] for r in reports if region_of(r['SO']) == r['SO']}
    status = [(region, by_region.get(region)) for region in _REGION_ORDER]
    return status, [r['SO'] for r in reports if r['SO'] not in by_region]


def latest_draft(messages):
    """가장 최근 취합안 답변(없으면 None)."""
    return next((m for m in reversed(messages) if m.get('draft') and m.get('role') == 'assistant'), None)


def draft_is_stale(messages):
    """취합안을 만든 뒤에 SO 보고서가 새로 정리되거나 교체됐으면 True(그 SO는 취합안에 반영되지 않았다)."""
    draft = latest_draft(messages)
    if draft is None:
        return False
    after = messages[next(i for i, m in enumerate(messages) if m is draft) + 1:]
    return any(m.get('report_meta') for m in after)


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
    globals()['generate_report_reply'] = lambda history, ctx, so='': calls.append((history, so)) or next(replies)
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
    status, extra = region_status(reports)
    assert [s for _, s in status] == ["대전", None, None, "광주", None, None, None, "대구"] and extra == ["SO 미확인 1", "SO 미확인 2", "본사"], (status, extra)
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
    print("report_service self-check OK")
