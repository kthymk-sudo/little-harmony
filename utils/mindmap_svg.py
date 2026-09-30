# utils/mindmap_svg.py
# ============================================================
# 🌟 [알마인드 미리보기] 보고서 트리([(depth, text), ...])를 그림(SVG)으로 그려서, .emm 파일을 열기 전에도 알마인드에서 어떤 모양으로
# 나올지 대화 화면에서 바로 볼 수 있게 한다. 알마인드 가지형(오른쪽 계층형)처럼 중심 토픽이 왼쪽, 가지가 오른쪽으로 뻗는다.
# 실제 알마인드 화면과 글꼴·간격은 조금 다를 수 있다. Streamlit에 의존하지 않는 순수 함수라 테스트할 수 있다.
# ============================================================
import html

_ROW_H, _ROW_GAP, _PAD, _COL_GAP = 26, 8, 12, 46
_STYLES = [  # depth별 (채움색, 테두리색, 글자 굵기, 글자 크기)
    ("#FFFFFF", "#1B2333", 700, 14),
    ("#EAF5C9", "#A6CE39", 600, 13),
    ("#FFFFFF", "#CFE39A", 400, 12),
]


def _text_px(text, size):
    """글자 폭 추정(한글·전각은 글자 크기만큼, 영문·숫자는 절반 남짓)."""
    return sum(size if ord(c) > 0x2E80 else size * 0.58 for c in text)


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _build(nodes, max_depth):
    """[(depth, text)] → 자식 목록 트리. 반환: (루트 리스트, 숨겨진 항목 수). 루트가 여럿이면 호출부가 가짜 중심을 씌운다."""
    roots, stack, hidden = [], [], 0
    for depth, text in nodes:
        if depth > max_depth:
            hidden += 1
            continue
        node = {"text": text, "depth": depth, "kids": []}
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        (stack[-1]["kids"] if stack else roots).append(node)
        stack.append(node)
    return roots, hidden


def mindmap_svg(nodes, title="취합", max_depth=3, max_label=30):
    """트리를 SVG 문자열로. max_depth보다 깊은 항목은 그리지 않고 개수만 돌려준다: (svg, 숨긴 항목 수). 항목이 없으면 ("", 0)."""
    roots, hidden = _build(nodes, max_depth)
    if not roots:
        return "", 0
    if len(roots) > 1:   # 📌 중심이 여러 개(SO별 기본 취합): 공통 중심을 씌우고 각 📌가 1단계 가지가 된다
        def shift(n):
            n["depth"] += 1
            for k in n["kids"]:
                shift(k)
        for r in roots:
            shift(r)
        roots = [{"text": title, "depth": 0, "kids": roots}]
    root = roots[0]

    widths = {}

    def measure(n):
        size = _STYLES[min(n["depth"], 2)][3]
        n["label"] = _clip(n["text"], max_label if n["depth"] else 40)
        n["w"] = _text_px(n["label"], size) + 2 * _PAD
        widths[n["depth"]] = max(widths.get(n["depth"], 0), n["w"])
        for k in n["kids"]:
            measure(k)

    measure(root)
    xs, x = {}, _PAD
    for d in range(max(widths) + 1):
        xs[d] = x
        x += widths[d] + _COL_GAP

    cursor = [_PAD]

    def place(n):
        if not n["kids"]:
            n["y"] = cursor[0] + _ROW_H / 2
            cursor[0] += _ROW_H + _ROW_GAP
        else:
            for k in n["kids"]:
                place(k)
            n["y"] = (n["kids"][0]["y"] + n["kids"][-1]["y"]) / 2

    place(root)
    width, height = int(x - _COL_GAP + _PAD), int(cursor[0])
    lines, boxes = [], []

    def draw(n):
        fill, stroke, weight, size = _STYLES[min(n["depth"], 2)]
        nx, ny = xs[n["depth"]], n["y"]
        for k in n["kids"]:
            x1, x2, y2 = nx + n["w"], xs[k["depth"]], k["y"]
            mid = (x1 + x2) / 2
            lines.append(f'<path d="M{x1:.0f} {ny:.0f} C{mid:.0f} {ny:.0f} {mid:.0f} {y2:.0f} {x2:.0f} {y2:.0f}" '
                         f'stroke="#A6CE39" stroke-width="2" fill="none"/>')
            draw(k)
        boxes.append(
            f'<g><title>{html.escape(n["text"])}</title>'
            f'<rect x="{nx:.0f}" y="{ny - _ROW_H / 2:.0f}" width="{n["w"]:.0f}" height="{_ROW_H}" rx="7" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{2 if n["depth"] == 0 else 1}"/>'
            f'<text x="{nx + _PAD:.0f}" y="{ny + size * 0.35:.0f}" font-size="{size}" font-weight="{weight}" fill="#1B2333" '
            f'font-family="Malgun Gothic, Source Sans, sans-serif">{html.escape(n["label"])}</text></g>')

    draw(root)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
           + "".join(lines) + "".join(boxes) + "</svg>")
    return svg, hidden


if __name__ == "__main__":
    nodes = [(0, "9월 4주차 활동 보고 취합"), (1, "짬짬반장 발굴"), (2, "대전"), (3, "신규 참여 경로당 : 1개소"), (3, "※ 인수인계 진행 중"),
             (2, "광주"), (3, "힐스테이트3단지 촬영 동의 완료"), (1, "조회수"), (2, "대전"), (3, "14회 → 70회"), (4, "너무 깊은 항목 <b>")]
    svg, hidden = mindmap_svg(nodes)
    assert svg.startswith("<svg") and svg.endswith("</svg>") and hidden == 1 and svg.count("<rect") == 10 and svg.count("<path") == 9
    assert "너무 깊은" not in svg
    full, hidden4 = mindmap_svg(nodes, max_depth=4)
    assert hidden4 == 0 and "너무 깊은 항목 &lt;b&gt;" in full and "<b>" not in full, "글자는 이스케이프된다"
    # 루트가 여럿이면 공통 중심을 씌운다 / 빈 입력 / 긴 글은 줄임표와 전체 글 툴팁
    multi, _ = mindmap_svg([(0, "대전"), (1, "실적"), (0, "광주"), (1, "실적")], title="SO별 취합")
    assert multi.count("<rect") == 5 and "SO별 취합" in multi
    assert mindmap_svg([]) == ("", 0)
    long, _ = mindmap_svg([(0, "중심"), (1, "가" * 60)])
    assert "…" in long and ("가" * 60) in long
    print("mindmap_svg self-check OK")
