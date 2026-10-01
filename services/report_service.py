# services/report_service.py
# ============================================================
# 📝 보고서 탭의 자료 다루기 (Streamlit 비의존 - 단위 테스트 가능). 대화 한 턴을 처리하는 AI는 services/report_agent.py에 있다.
#  - 원문 대조: find_missing_items가 정리 결과에 원문의 링크·숫자·문장이 빠짐없이 들어 있는지 찾는다(AI에게 돌려줘서 스스로 고치게 한다).
#  - SO별 취합: SO 보고서 답변에는 어느 SO·기간인지(report_meta)를 저장해 두고(collect_so_reports) 취합본·취합안·분석/첨부 자료를 모은다.
# ============================================================
import re

from ai_engine.gemini_api import read_file_text
from utils.file_reader import read_attachment, AttachmentError
from config import SO_REGIONS, REGION_ORDER as _REGION_ORDER
from utils.emm_export import parse_report_tree

_URL = re.compile(r"https?://[^\s)\]>\"'）]+")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


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

    prior = [{"role": "user", "text": "대전 정리"}, {"role": "assistant", "text": good, "report_meta": {"SO": "대전", "기간": "9월 4주차"}}]
    assert latest_draft(prior) is None and "https://rainbowtv.app.link/tbVHa8ZYs6b" in so_reports_context(collect_so_reports(prior))
    d1 = {"role": "assistant", "text": "📌 취합", "draft": True}
    assert latest_draft(prior + [d1]) is d1 and not draft_is_stale(prior + [d1]) and draft_is_stale([d1] + prior[1:]) and not draft_is_stale([_msg("충청", "9월")])
    # 분석 자료: 표는 앞 행만, 자료는 재료로 전달되고, 취합안의 숫자는 자료 본문과도 대조되며, 제외하면 빠진다. 자료가 나중에 담기면 취합안은 낡은 것
    mat = build_material("6월 SO별 MAU", {"text": "대전 688명이 가장 많아요.", "table": [{"SO": "대전", "MAU": 688}, {"SO": "충청", "MAU": 71}]}, max_rows=1)
    assert mat["제목"] == "6월 SO별 MAU" and "SO | MAU\n대전 | 688" in mat["본문"] and "충청" not in mat["본문"] and "2행 중 1행" in mat["본문"], mat
    note = {"role": "assistant", "text": "담았어요", "material": mat}
    with_mat = prior + [note]
    assert [m["index"] for m in collect_materials(with_mat)] == [2] and collect_materials(with_mat, excluded=[2]) == []
    assert "[분석 자료" in so_reports_context([], collect_materials(with_mat)) and "[분석 자료" not in so_reports_context([], [])
    note = {"role": "assistant", "text": "담았어요", "material": mat}
    assert draft_is_stale([d1, note]) and not draft_is_stale([note, d1])
    # 올린 파일 읽기: 읽은 것과 못 읽은 사유를 나눠 돌려준다(사진은 AI가 글자를 읽는다)
    globals()['read_file_text'] = lambda data, mime: "사진 속 글자 70회"
    ok_files, bad_files = read_attachments([("a.txt", "안녕".encode()), ("b.png", b"x"), ("c.exe", b"x")])
    assert [a["제목"] for a in ok_files] == ["a.txt", "b.png"] and ok_files[1]["본문"] == "사진 속 글자 70회" and len(bad_files) == 1 and "c.exe" in bad_files[0]
    print("report_service self-check OK")
