# utils/emm_export.py
# ============================================================
# 📝 보고서를 실제 알마인드(ALMind, .emm) 파일로 내보내기.
#
# 기준 파일은 알마인드 설치 폴더의 "가지형" 테마 원본 템플릿
# (Contents/KOR/Templates/aBranchMap.emmt → assets/almind_template/
# branch_base.emmt)이다. 알마인드에서 [새 시트 → 테마 맵 → 가지형]을
# 고르면 쓰이는 바로 그 파일이라, 도형/가지선/색/배경은 알마인드가 만든
# 그대로 두고 아래 사용자 양식만 덮어쓴다.
#   - 진행방향: 오른쪽 계층형 (ClassRight)
#   - 가지 모양: 구부러진선 (RoundedElbow)
#   - 글꼴: 맑은 고딕, 중심토픽 12pt 굵게 / 나머지 9pt
#   - 크기 및 위치 > 여백(내부) 상하좌우 1.38mm
#   - 토픽 자동 맞춤 ON, 토픽 자동 줄바꿈 OFF
#   - 배경: 가지형 기본값이 이미 채우기 없음(투명)
#
# 🌟 메뉴 이름 ↔ 파일 값 대응은 추측하지 않고 확인했다: 알마인드 UI의
# 진행방향/가지 모양 메뉴 항목 순서가 HMindmapApp.dll 안의 값 목록 순서와
# 정확히 일치한다(오른쪽 계층형=ClassRight, 오른쪽 방사형=MapRight,
# 구부러진선=RoundedElbow, 곡선=Curve, 호=Arc ...).
#
# 🌟 구조 규칙(실제 알마인드로 열어보며 확인):
#   - mapHead 뒤 signManager 등 템플릿 앞부분은 그대로 둬야 열린다.
#   - <m:textBox> 안에 <m:element m:id="1">이 반드시 있어야 한다.
#   - m:elementCount는 그림 등 부속 요소 개수라 텍스트 토픽은 항상 0.
#   - zip은 원본 항목을 한 번씩만 써서 새로 만든다(같은 이름을 append로
#     덧쓰면 알마인드가 먼저 나온 원본을 읽어 내용이 반영되지 않는다).
#   - 글꼴 크기는 새 스타일을 추가하지 않고 템플릿의 기존 글자 스타일
#     값만 바꾼다(가장 안전하고, 사용자가 새 토픽을 추가해도 같은 양식).
# ============================================================
import io
import re
import uuid
import zipfile
from pathlib import Path

_BASE_PATH = Path(__file__).resolve().parent.parent / "assets" / "almind_template" / "branch_base.emmt"

_GROWTH_DIRECTION = "ClassRight"   # 오른쪽 계층형
_BRANCH_SHAPE = "RoundedElbow"     # 구부러진선
_MARGIN = "1.38mm"
_TEXT_WRAP_WIDTH = "96.519mm"

# 가지형 템플릿 mapMaster1.xml의 m:central 레벨 정의 그대로(도형/가지 스타일 id,
# 기본 크기, 글자 스타일 id). depth가 더 깊으면 마지막 항목을 쓴다.
_LEVELS = [
    {"catalog": "RoundRectangle", "topic_psid": 7, "fiber_psid": 17, "char": 4,
     "width": 26.920, "height": 9.720, "anchor": "Center", "picture_wrap": "Top", "spacing_parent": "7mm"},
    {"catalog": "Underline", "topic_psid": 12, "fiber_psid": 3, "char": 5,
     "width": 19.950, "height": 7.960, "anchor": "Outside", "picture_wrap": "Left", "spacing_parent": "2mm"},
    {"catalog": "Underline", "topic_psid": 13, "fiber_psid": 3, "char": 1,
     "width": 17.819, "height": 6.050, "anchor": "Outside", "picture_wrap": "Left", "spacing_parent": "2mm"},
    {"catalog": "Underline", "topic_psid": 13, "fiber_psid": 3, "char": 3,
     "width": 16.760, "height": 5.690, "anchor": "Outside", "picture_wrap": "Left", "spacing_parent": "2mm"},
]

# 템플릿 shape.xml 글자 스타일 id -> 바꿀 크기 (4: 중심토픽 14pt 굵게,
# 5: 주요토픽 11pt, 1: 하위토픽 10pt, 3: 그 아래 9pt). 전부 맑은 고딕.
_CHAR_SIZES = {4: "12pt", 5: "9pt", 1: "9pt", 3: "9pt"}


def _new_uuid():
    return "{%s}" % str(uuid.uuid4()).upper()


def _xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def parse_report_tree(report_text):
    """보고서 답변 텍스트(📌/▸/· 들여쓰기 트리 + 피드백)에서 트리 부분만 뽑아
    [(depth, text), ...] 리스트로 변환한다. 피드백/데이터 인사이트 문단은 제외."""
    tree_lines = []
    for line in report_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("💡") or stripped.startswith("📊"):
            break
        if stripped.startswith("📌"):
            tree_lines.append((0, stripped.lstrip("📌").strip()))
        elif stripped.startswith("▸"):
            tree_lines.append((1, stripped.lstrip("▸").strip()))
        elif stripped.startswith("·"):
            tree_lines.append((2, stripped.lstrip("·").strip()))
    return tree_lines


def _mm(value):
    return f"{value:.3f}mm"


def _build_topic_xml(text, depth, children_xml):
    lv = _LEVELS[min(depth, len(_LEVELS) - 1)]
    margin = float(_MARGIN.replace("mm", ""))
    width, height = lv["width"], lv["height"]
    inner_w, inner_h = width - 2 * margin, height - 2 * margin
    is_root = depth == 0
    growth_direction = _GROWTH_DIRECTION if is_root else "Auto"
    growth_side = "Right"
    fiber_catalog = _BRANCH_SHAPE

    return (
        f'<m:topic m:uuid="{_new_uuid()}" m:mainElementCount="1" m:mainElementKind="TextBox" m:elementCount="0" '
        f'm:topicCatalog="{lv["catalog"]}" m:fiberCatalog="{fiber_catalog}" m:outlineShapeID="0" '
        f'm:topicPSID="{lv["topic_psid"]}" m:fiberPSID="{lv["fiber_psid"]}" m:kind="Topic" '
        f'm:growthDirection="{growth_direction}" m:outline="0" m:outlineDepth="0" m:outlineLevel="0" '
        f'm:branchAnchor="{lv["anchor"]}" m:autoTextWrap="false" m:noteUnroll="0" m:growthSplit="0" '
        f'm:userLayout="0" m:collapse="0" m:growthSide="{growth_side}">'
        f'<m:component m:x="0mm" m:y="0mm" m:orgWidth="{_mm(width)}" m:orgHeight="{_mm(height)}" '
        f'm:Width="{_mm(width)}" m:Height="{_mm(height)}"><m:renderingInfo m:e1="1.00000" m:e5="1.00000" /></m:component>'
        f'<m:layout m:width="{_mm(width)}" m:height="{_mm(height)}" m:widthRoll="{_mm(width)}" m:heightRoll="{_mm(height)}" '
        f'm:widthUnroll="{_mm(width)}" m:heightUnroll="{_mm(height)}" m:textWrapWidth="{_TEXT_WRAP_WIDTH}" '
        f'm:innerWidth="{_mm(inner_w)}" m:innerHeigth="{_mm(inner_h)}" '
        f'm:marginLeft="{_MARGIN}" m:marginRight="{_MARGIN}" m:marginTop="{_MARGIN}" m:marginBottom="{_MARGIN}" '
        f'm:vertLocalOffset="0mm" m:horzLocalOffset="0mm" m:spacingParent="{lv["spacing_parent"]}" m:spacingSiblings="1.389mm" />'
        f'<m:pictureLayout m:pictureWrap="{lv["picture_wrap"]}" m:pictureHorzAlign="Center" m:pictureVertAlign="Center" />'
        f'<m:mainElement><m:textBox m:maxWidth="{_mm(inner_w)}">'
        f'<m:element m:id="1" m:width="{_mm(inner_w)}" m:height="{_mm(inner_h)}" m:marginLeft="0mm" m:marginRight="0mm" '
        f'm:marginTop="0mm" m:marginBottom="0mm"><m:component m:x="0mm" m:y="0mm" m:orgWidth="{_mm(inner_w)}" '
        f'm:orgHeight="{_mm(inner_h)}" m:Width="{_mm(inner_w)}" m:Height="{_mm(inner_h)}">'
        f'<m:renderingInfo m:e1="1.00000" m:e5="1.00000" /></m:component></m:element>'
        f'<m:textMargin m:left="0mm" m:right="0mm" m:top="0mm" m:bottom="0mm" />'
        f'<m:paraList m:uuid="{_new_uuid()}" m:wrap="Auto" m:vertAlign="Center"><m:p m:paraShape="1">'
        f'<m:text m:charShape="{lv["char"]}"><m:char>{_xml_escape(text)}</m:char></m:text></m:p></m:paraList>'
        f'</m:textBox></m:mainElement>'
        f'{children_xml}</m:topic>'
    )


def _build_branch(nodes, index, depth):
    """nodes: [(depth, text), ...]를 topicBranch/topic 중첩 XML로 조립한다.
    반환: (topicBranch XML 또는 자식이 없으면 빈 문자열, 다음 index)"""
    children = []
    while index < len(nodes) and nodes[index][0] == depth:
        text = nodes[index][1]
        index += 1
        grandchildren_xml, index = _build_branch(nodes, index, depth + 1)
        children.append(_build_topic_xml(text, depth, grandchildren_xml))
    if not children:
        return "", index
    return f'<m:topicBranch m:count="{len(children)}">' + "".join(children) + "</m:topicBranch>", index


def _build_map1_xml(template_map1, title, tree_nodes):
    """템플릿 map1.xml에서 토픽 트리 앞부분(mapHead/signManager 등)은 그대로
    두고, 토픽 트리만 새로 만든다. 📌가 하나면 그 문구가 중심토픽이 되고,
    여러 개면 title로 중심토픽을 만들어 그 아래에 전부 붙인다."""
    top_level = [n for n in tree_nodes if n[0] == 0]
    if len(top_level) == 1:
        center_text = top_level[0][1]
        children_xml, _ = _build_branch(tree_nodes, 1, 1)
    else:
        center_text = title
        shifted = [(depth + 1, text) for depth, text in tree_nodes]
        children_xml, _ = _build_branch(shifted, 0, 1)
    root_xml = _build_topic_xml(center_text, 0, children_xml)

    prefix = template_map1[:template_map1.index("<m:topicBranch")]
    prefix = re.sub(r'mapHead m:uuid="\{[0-9A-F-]+\}"', f'mapHead m:uuid="{_new_uuid()}"', prefix, count=1)
    prefix = re.sub(r"<m:title>.*?</m:title>", f"<m:title>{_xml_escape(title)}</m:title>", prefix, count=1, flags=re.S)
    return f'{prefix}<m:topicBranch m:count="1">{root_xml}</m:topicBranch></Map>'


def _patch_shape_xml(shape_xml):
    """기존 글자 스타일의 크기만 바꾼다(새 항목 추가 없음)."""
    for char_id, size in _CHAR_SIZES.items():
        shape_xml, n = re.subn(rf'(<m:charshape m:id="{char_id}" m:size=")[^"]+(")', rf"\g<1>{size}\g<2>", shape_xml, count=1)
        if n != 1:
            raise ValueError(f"템플릿 글자 스타일 {char_id}번을 찾지 못했습니다.")
    return shape_xml


def _patch_map_master(master_xml):
    """사용자가 알마인드에서 토픽을 새로 추가해도 같은 양식이 나오도록
    가지형(m:central) 레벨 기본값에도 여백/줄바꿈/가지 모양/진행방향을 반영한다."""
    start, end = master_xml.index("<m:central>"), master_xml.index("</m:central>")
    central = master_xml[start:end]
    central = re.sub(r'm:margin(Left|Right|Top|Bottom)="[^"]+"', rf'm:margin\1="{_MARGIN}"', central)
    central = central.replace('m:autoTextWrap="true"', 'm:autoTextWrap="false"')
    central = re.sub(r'm:fiberCatalog="(Curve|Branch)"', f'm:fiberCatalog="{_BRANCH_SHAPE}"', central)
    level0_end = central.index("</m:level0>")
    central = central[:level0_end].replace('m:growthDirection="Auto"', f'm:growthDirection="{_GROWTH_DIRECTION}"', 1) + central[level0_end:]
    return master_xml[:start] + central + master_xml[end:]


def _patch_docprops(app_xml, core_xml, title, topic_count):
    app_xml = re.sub(r'm:map="[^"]*"', f'm:map="{_xml_escape(title)}"', app_xml, count=1)
    app_xml = re.sub(r"<m:topics>\d+</m:topics>", f"<m:topics>{topic_count}</m:topics>", app_xml, count=1)
    core_xml = re.sub(r"<dc:title>.*?</dc:title>", f"<dc:title>{_xml_escape(title)}</dc:title>", core_xml, count=1, flags=re.S)
    return app_xml, core_xml


def build_emm_bytes(report_text, title):
    """보고서 답변 텍스트를 가지형 알마인드(.emm) 파일 바이트로 변환한다."""
    tree_nodes = parse_report_tree(report_text)
    if not tree_nodes:
        raise ValueError("보고서에서 트리 요약을 찾지 못해 알마인드 파일을 만들 수 없습니다.")

    with zipfile.ZipFile(_BASE_PATH) as src:
        entries = [(name, src.read(name)) for name in src.namelist()]
    files = dict(entries)

    def text(name):
        return files[name].decode("utf-8")

    app_xml, core_xml = _patch_docprops(text("docProps/app.xml"), text("docProps/core.xml"), title, len(tree_nodes) + 1)
    overrides = {
        "map/maps/map1.xml": _build_map1_xml(text("map/maps/map1.xml"), title, tree_nodes),
        "map/shape/shape.xml": _patch_shape_xml(text("map/shape/shape.xml")),
        "map/maps/mapMaster1.xml": _patch_map_master(text("map/maps/mapMaster1.xml")),
        "docProps/app.xml": app_xml,
        "docProps/core.xml": core_xml,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries:
            out.writestr(name, overrides.get(name, data))
    return buffer.getvalue()


if __name__ == "__main__":
    import xml.etree.ElementTree as ET

    sample = "📌 중심\n  ▸ 가\n    · 가-1\n    · 가-2\n  ▸ 나 & <다>\n\n💡 피드백\n무시"
    with zipfile.ZipFile(io.BytesIO(build_emm_bytes(sample, "테스트"))) as zf:
        assert len(zf.namelist()) == len(set(zf.namelist())), "중복 항목 금지"
        for name in zf.namelist():
            if name.endswith(".xml"):
                ET.fromstring(zf.read(name))
        map1 = zf.read("map/maps/map1.xml").decode("utf-8")
        shape = zf.read("map/shape/shape.xml").decode("utf-8")
    assert re.findall(r"<m:char>(.*?)</m:char>", map1) == ["중심", "가", "가-1", "가-2", "나 &amp; &lt;다&gt;"]
    assert map1.count('m:growthDirection="ClassRight"') == 1
    assert map1.count('m:fiberCatalog="RoundedElbow"') == 5
    assert map1.count("<m:element m:id=\"1\"") == 5
    assert '<m:charshape m:id="4" m:size="12pt"' in shape and '<m:charshape m:id="5" m:size="9pt"' in shape
    print("emm_export self-check OK")
