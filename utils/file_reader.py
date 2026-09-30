# utils/file_reader.py
# ============================================================
# 🌟 [보고서 첨부] 보고서 탭에 올린 파일에서 AI가 읽을 텍스트를 뽑는다.
#  - 알마인드(.emm): 토픽 트리를 들여쓰기 목록으로 (우리가 만드는 .emm과 같은 EMMX 형식)
#  - 엑셀(.xlsx)/CSV: 시트별로 한 행을 한 줄("값 | 값")로, 너무 크면 앞부분만
#  - 워드(.docx)/한글(.hwpx, .hwp): 문단을 한 줄씩, 표는 한 행을 "칸 | 칸"으로 (.hwp는 olefile로 본문 스트림을 읽는다)
#  - 사진/PDF: 글자를 읽는 일은 AI(ocr 함수)에게 맡긴다 - 이 모듈은 Streamlit·AI에 의존하지 않아 테스트할 수 있다
# 회사 문서 보안(DRM)이 걸린 파일은 열 수 없으며, 우회하지 않고 원본에서 보안을 해제해 올리도록 안내만 한다.
# ============================================================
import io
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

import pandas as pd

FILE_TYPES = ["emm", "xlsx", "csv", "txt", "md", "docx", "hwpx", "hwp", "png", "jpg", "jpeg", "webp", "pdf"]
MAX_CHARS = 30000        # 파일 하나에서 AI에게 넘기는 최대 글자 수
_MAX_ROWS, _MAX_COLS = 300, 30
_MAX_MB = {"image": 8, "pdf": 15, "other": 20}
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".pdf": "application/pdf"}
_NS = "{http://www.h2soft.com/EMMX}"
_DRM_HINT = "문서 보안(DRM)이 걸려 있으면 원본에서 보안을 해제한 파일을 올려 주세요."


class AttachmentError(Exception):
    """사용자에게 그대로 보여줘도 되는 읽기 실패 사유."""


def _clip(text):
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n(이하 생략: 전체 {len(text):,}자 중 앞 {MAX_CHARS:,}자만 읽었어요)"


def _decode(data):
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell(v):
    if pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _table_text(df):
    df = df.iloc[:_MAX_ROWS, :_MAX_COLS]
    lines = [" | ".join(_cell(v) for v in row).rstrip(" |") for row in df.itertuples(index=False)]
    return "\n".join(l for l in lines if l)


def excel_to_text(data):
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    parts = [f"[시트: {name}] ({len(df)}행)\n{_table_text(df)}" for name, df in sheets.items() if not df.empty]
    return "\n\n".join(parts)


def csv_to_text(data):
    return _table_text(pd.read_csv(io.StringIO(_decode(data)), header=None, dtype=str, keep_default_na=False))


def emm_to_text(data):
    """알마인드 파일의 토픽 트리를 '📌 중심 / - 가지' 들여쓰기 목록으로 바꾼다."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(n for n in z.namelist() if re.fullmatch(r"map/maps/map\d+\.xml", n))
        maps = [ET.fromstring(z.read(n)) for n in names]
    out = []

    def walk(topic, depth):
        main = topic.find(f"{_NS}mainElement")
        text = " ".join("".join(c.text or "" for c in p.iter(f"{_NS}char")) for p in main.iter(f"{_NS}p")) if main is not None else ""
        if text.strip():
            out.append("  " * depth + ("📌 " if depth == 0 else "- ") + text.strip())
        for branch in topic.findall(f"{_NS}topicBranch"):
            for child in branch.findall(f"{_NS}topic"):
                walk(child, depth + 1)

    for i, root in enumerate(maps):
        if len(maps) > 1:
            out.append(f"[맵 {i + 1}]")
        for branch in root.findall(f"{_NS}topicBranch"):
            for topic in branch.findall(f"{_NS}topic"):
                walk(topic, 0)
    return "\n".join(out)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _own_text(el):
    """문단 하나의 글자(표 안의 글자는 제외). 워드(w:t)와 한글(hp:t) 모두 글자 태그 이름이 t다."""
    parts = []

    def rec(e):
        name = _local(e.tag)
        if name == "tbl":
            return
        if name == "t":
            parts.append("".join(e.itertext()))
            return
        if name == "tab":
            parts.append("\t")
        for c in e:
            rec(c)

    rec(el)
    return "".join(parts).strip()


def _xml_doc_text(root):
    """워드/한글 XML의 문단(p)을 한 줄씩, 표(tbl)는 한 행(tr)을 '칸 | 칸'으로. 두 형식 모두 p/t/tbl/tr/tc 이름을 쓴다."""
    out = []

    def visit(el):
        name = _local(el.tag)
        if name == "tbl":
            for tr in el:
                if _local(tr.tag) == "tr":
                    cells = [" ".join(t for t in (_own_text(p) for p in tc.iter() if _local(p.tag) == "p") if t)
                             for tc in tr if _local(tc.tag) == "tc"]
                    if any(cells):
                        out.append(" | ".join(cells))
            return
        if name == "p":
            text = _own_text(el)
            if text:
                out.append(text)
        for c in el:
            visit(c)

    visit(root)
    return "\n".join(out)


def docx_to_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _xml_doc_text(ET.fromstring(z.read("word/document.xml")))


def hwpx_to_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\n".join(_xml_doc_text(ET.fromstring(z.read(n))) for n in names)


_HWP_INLINE_CTRL = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}  # 뒤에 7글자(14바이트) 부가정보가 붙는 제어문자
_HWP_LOCKED = (1 << 1) | (1 << 2) | (1 << 4) | (1 << 10)   # 암호, 배포용, DRM 보안, 공인인증서 DRM


def _hwp_paragraphs(raw):
    """HWP 본문 스트림의 레코드에서 문단 글자(PARA_TEXT, 태그 67)만 뽑는다."""
    out, pos = [], 0
    while pos + 4 <= len(raw):
        header = struct.unpack_from("<I", raw, pos)[0]
        tag, size = header & 0x3FF, header >> 20
        pos += 4
        if size == 0xFFF:
            size = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
        body = raw[pos:pos + size]
        pos += size
        if tag != 67:
            continue
        chars, i = [], 0
        while i + 2 <= len(body):
            code = struct.unpack_from("<H", body, i)[0]
            i += 2
            if code >= 32:
                chars.append(chr(code))
            elif code in (10, 13):
                chars.append("\n")
            elif code == 9:
                chars.append("\t")
            elif code in _HWP_INLINE_CTRL:
                i += 14
        text = "".join(chars).strip()
        if text:
            out.append(text)
    return "\n".join(out)


def hwp_to_text(data):
    try:
        import olefile
    except ImportError:
        raise AttachmentError(".hwp를 읽는 도구(olefile)가 설치돼 있지 않아요. 한글에서 .hwpx나 PDF로 저장해서 올려 주세요.")
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise ValueError("not hwp")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & _HWP_LOCKED:
            raise AttachmentError("암호·배포용·문서 보안(DRM)이 걸린 한글 문서라 읽을 수 없어요. 원본에서 보안을 해제해 올려 주세요.")
        sections = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")),
                          key=lambda e: int(e[1][7:]))
        texts = []
        for e in sections:
            raw = ole.openstream(e).read()
            texts.append(_hwp_paragraphs(zlib.decompress(raw, -15) if flags & 1 else raw))
        return "\n".join(texts)
    finally:
        ole.close()


def read_attachment(name, data, ocr=None):
    """파일 하나를 읽어 {'제목', '본문'}으로. 읽을 수 없으면 AttachmentError(사유).
    ocr(data, mime_type) -> str : 사진·PDF의 글자를 읽어 주는 함수(실패하면 '⚠️'로 시작하는 문구를 돌려줘도 된다)."""
    ext = os.path.splitext(name)[1].lower()
    kind = "image" if ext in (".png", ".jpg", ".jpeg", ".webp") else "pdf" if ext == ".pdf" else "other"
    if ext.lstrip(".") not in FILE_TYPES:
        raise AttachmentError(f"{name}: 올릴 수 없는 파일 형식이에요 ({', '.join(FILE_TYPES)}만 가능해요).")
    if len(data) > _MAX_MB[kind] * 1024 * 1024:
        raise AttachmentError(f"{name}: 파일이 너무 커요 ({_MAX_MB[kind]}MB 이하만 읽을 수 있어요).")
    try:
        if ext in _MIME:
            if ocr is None:
                raise AttachmentError(f"{name}: 사진·PDF를 읽을 수 없는 상태예요.")
            text = ocr(data, _MIME[ext])
            if text.lstrip().startswith("⚠️"):
                raise AttachmentError(f"{name}: 사진·PDF의 글자를 읽지 못했어요. 잠시 뒤 다시 올려 주세요.")
        elif ext == ".emm":
            text = emm_to_text(data)
        elif ext == ".xlsx":
            text = excel_to_text(data)
        elif ext == ".csv":
            text = csv_to_text(data)
        elif ext == ".docx":
            text = docx_to_text(data)
        elif ext == ".hwpx":
            text = hwpx_to_text(data)
        elif ext == ".hwp":
            text = hwp_to_text(data)
        else:
            text = _decode(data)
    except AttachmentError as e:
        raise AttachmentError(str(e) if str(e).startswith(name) else f"{name}: {e}")
    except Exception:  # 손상됐거나 보안이 걸린 파일 등
        raise AttachmentError(f"{name}: 파일을 열지 못했어요. {_DRM_HINT}")
    if not text.strip():
        raise AttachmentError(f"{name}: 읽을 수 있는 내용이 없어요.")
    return {"제목": name, "본문": _clip(text.strip())}


def _selfcheck_docx_hwpx():
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    docx = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>9월 4주차 활동 보고</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>1. 캠페인 진행</w:t></w:r><w:r><w:tab/><w:t>(9/16~9/22)</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>SO</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>MAU</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:p><w:r><w:t>대전</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>688</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>끝</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", docx)
    got = read_attachment("보고.docx", buf.getvalue())["본문"]
    assert got == "9월 4주차 활동 보고\n1. 캠페인 진행\t(9/16~9/22)\nSO | MAU\n대전 | 688\n끝", got

    H = "http://www.hancom.co.kr/hwpml/2011/paragraph"
    sec = (f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="{H}">'
           '<hp:p><hp:run><hp:t>대전 SO 보고</hp:t></hp:run></hp:p>'
           '<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>구분</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
           '<hp:tc><hp:subList><hp:p><hp:run><hp:t>횟수</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>'
           '<hp:p><hp:run><hp:t>참여 13개소</hp:t></hp:run></hp:p></hs:sec>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Contents/section1.xml", sec.replace("참여 13개소", "2쪽 내용"))
        z.writestr("Contents/section0.xml", sec)
    got = read_attachment("보고.hwpx", buf.getvalue())["본문"]
    assert got == "대전 SO 보고\n구분 | 횟수\n참여 13개소\n대전 SO 보고\n구분 | 횟수\n2쪽 내용", got


def _selfcheck_hwp():
    """실제 .hwp와 같은 구조(OLE 복합 파일: FileHeader + BodyText/Section0, 본문은 압축된 레코드)를 만들어 읽어 본다."""
    def rec(tag, payload):
        return struct.pack("<I", tag | (len(payload) << 20)) + payload

    def para(text):  # 제어문자(구역 정의 등 확장 컨트롤 16바이트)가 앞에 붙은 문단
        ctrl = struct.pack("<H", 2) + b"\x00" * 14
        return rec(67, ctrl + text.encode("utf-16-le") + struct.pack("<H", 13))

    raw = para("9월 4주차 활동 보고") + rec(66, b"\x00" * 8) + para("1. 캠페인 진행 : 13개소")
    assert _hwp_paragraphs(raw) == "9월 4주차 활동 보고\n1. 캠페인 진행 : 13개소"
    co = zlib.compressobj(wbits=-15)
    packed = co.compress(raw) + co.flush()

    def ole_file(flags):
        header_stream = (b"HWP Document File" + b"\x00" * 15 + struct.pack("<II", 0x05000300, flags)).ljust(4096, b"\x00")
        section = packed.ljust(4096, b"\x00")   # 4096바이트 이상이라 미니 스트림을 쓰지 않는다
        sec = 512
        # 구역(섹터) 배치: 0=FAT, 1=디렉터리, 2..9=FileHeader, 10..17=Section0
        fat = [0xFFFFFFFD, 0xFFFFFFFE] + list(range(3, 10)) + [0xFFFFFFFE] + list(range(11, 18)) + [0xFFFFFFFE]
        fat_sector = b"".join(struct.pack("<I", x) for x in fat).ljust(sec, b"\xff")

        def entry(name, kind, left, right, child, start, size):
            nm = name.encode("utf-16-le") + b"\x00\x00"
            return (nm.ljust(64, b"\x00") + struct.pack("<HBB", len(nm), kind, 1) + struct.pack("<III", left, right, child)
                    + b"\x00" * 16 + struct.pack("<I", 0) + b"\x00" * 16 + struct.pack("<IQ", start, size))
        NIL = 0xFFFFFFFF
        entries = [entry("Root Entry", 5, NIL, NIL, 3, 0xFFFFFFFE, 0),      # 0: 루트 → 자식 3(BodyText)
                   entry("FileHeader", 2, NIL, NIL, NIL, 2, len(header_stream)),  # 1
                   entry("Section0", 2, NIL, NIL, NIL, 10, len(section)),         # 2
                   entry("BodyText", 1, NIL, 1, 2, 0, 0)]                          # 3: 오른쪽 형제 1(FileHeader), 자식 2(Section0)
        directory = b"".join(entries).ljust(sec, b"\x00")
        difat = struct.pack("<I", 0) + b"\xff" * (4 * 108)
        head = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
                + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0) + difat)
        return head + fat_sector + directory + header_stream + section

    assert read_attachment("보고.hwp", ole_file(1))["본문"] == "9월 4주차 활동 보고\n1. 캠페인 진행 : 13개소"
    for locked in (2, 4, 16):   # 암호 / 배포용 / DRM
        try:
            read_attachment("잠김.hwp", ole_file(1 | locked))
            raise SystemExit("잠긴 문서는 오류여야 함")
        except AttachmentError as e:
            assert "잠김.hwp" in str(e) and "읽을 수 없어요" in str(e), str(e)


if __name__ == "__main__":
    from utils.emm_export import build_emm_from_nodes

    nodes = [(0, "대전 · 9월 4주차 활동 보고"), (1, "1. 캠페인 진행"), (2, "콘텐츠 경로 : https://a.b/c"), (1, "2. 실적"), (2, "14회 → 70회"), (3, "※ 400% 증가")]
    emm = read_attachment("대전.emm", build_emm_from_nodes(nodes, "제목"))["본문"]
    assert emm == "📌 대전 · 9월 4주차 활동 보고\n  - 1. 캠페인 진행\n    - 콘텐츠 경로 : https://a.b/c\n  - 2. 실적\n    - 14회 → 70회\n      - ※ 400% 증가", emm

    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        pd.DataFrame({"SO": ["대전", "광주"], "MAU": [688, 313.0], "비고": [None, "확인"]}).to_excel(w, sheet_name="실적", index=False)
        pd.DataFrame().to_excel(w, sheet_name="빈시트", index=False)
    xl = read_attachment("실적.xlsx", buf.getvalue())["본문"]
    assert xl == "[시트: 실적] (3행)\nSO | MAU | 비고\n대전 | 688\n광주 | 313 | 확인", xl

    assert read_attachment("a.csv", "SO,MAU\n대전,688\n".encode("cp949"))["본문"] == "SO | MAU\n대전 | 688"
    assert read_attachment("a.txt", "안녕".encode("utf-8-sig"))["본문"] == "안녕"
    assert read_attachment("표.png", b"x", ocr=lambda d, m: f"읽음 {m}")["본문"] == "읽음 image/png"
    assert read_attachment("긴글.txt", ("가" * (MAX_CHARS + 5)).encode())["본문"].endswith("만 읽었어요)")
    _selfcheck_docx_hwpx()
    _selfcheck_hwp()

    for name, data, kw in [("a.exe", b"x", "형식"), ("b.png", b"x", "읽을 수 없는"), ("c.emm", b"not a zip", "DRM"),
                           ("d.xlsx", b"garbage", "DRM"), ("e.txt", b"   ", "내용이 없어요"), ("f.png", b"x" * (9 * 1024 * 1024), "너무 커요"),
                           ("g.docx", b"encrypted", "DRM"), ("h.hwp", b"encrypted", "DRM"), ("i.hwpx", b"encrypted", "DRM")]:
        try:
            read_attachment(name, data)
            raise SystemExit(f"오류가 나야 함: {name}")
        except AttachmentError as e:
            assert kw in str(e), (name, str(e))
    try:
        read_attachment("g.png", b"x", ocr=lambda d, m: "⚠️ 실패")
        raise SystemExit("ocr 실패는 오류여야 함")
    except AttachmentError as e:
        assert "읽지 못했어요" in str(e)
    print("file_reader self-check OK")
