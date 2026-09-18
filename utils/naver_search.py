# utils/naver_search.py
# ============================================================
# 🌟 [신조어/속어 이해 고도화] "꽃중년"처럼 AI가 자기 지식만으로는 정확한 뜻을
# 확신하기 어려운 표현이 실무자 메시지에 나올 때, 추측 대신 실제 웹 검색으로 뜻을
# 확인하기 위한 모듈. 구글 제미나이 API의 자체 "구글 검색 연동(grounding)" 기능도
# 검토했으나 무료 등급에서는 아예 지원되지 않아(유료 결제 계정 필수) 채택하지 않았다.
#
# 🌟 [2026-09 기준 현재 상태 - 의도적으로 비활성] 대안으로 무료인 네이버 검색 API를
# 붙여뒀으나, 네이버가 신규 애플리케이션 등록을 기존 개발자센터 방식에서 "NAVER API
# HUB"로 이전 중이라(2026-07-31 이후 신규 앱은 개발자센터에서 발급 불가) 지금 당장
# 발급받기 번거롭다는 판단 하에, 실무자가 이 연동 자체를 당분간 보류하기로 했다.
# 그래서 .env에 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET을 넣지 않는 한 이 모듈은 항상
# None을 반환하며, 그 경우 호출부(services/target_service.py)는 AI가 확신 없는 단어를
# 실무자에게 직접 되묻는 대화로 자연스럽게 넘어간다(앱이 멈추거나 기능이 깨지지 않음 -
# 검색은 "있으면 더 정확해지는" 보조 기능일 뿐, 없어도 정상 동작한다).
#
# 나중에 검색 연동을 다시 고려한다면, 국립국어원 "우리말샘" 오픈API(무료, 신조어/속어
# 등재가 목적이라 "꽃중년" 같은 표현도 실제로 등재돼 있음, 개인 휴대폰 인증만으로 발급)
# 나 구글 Custom Search JSON API(하루 100건 무료)로 손쉽게 교체할 수 있도록,
# search_term_meaning(term) 하나의 함수 인터페이스만 지키면 이 파일의 내부 구현만
# 바꿔 끼우면 된다(호출부 코드는 전혀 손댈 필요 없음).
#
# API 키가 없거나 호출이 실패해도 예외를 던지지 않고 None을 반환한다 - 이 기능은
# "있으면 더 정확해지는" 보조 기능이지, 없다고 앱이 멈추면 안 되는 핵심 기능이 아니다
# (키를 아직 발급받지 못한 실무자도 기존처럼 AI가 직접 되물어보는 방식으로 계속 쓸 수 있음).
# ============================================================
import os
import re
import requests
from dotenv import load_dotenv

# 🌟 config.py가 먼저 임포트되는 정상 실행 흐름에서는 이미 로드되어 있지만, 이 모듈만
# 단독으로 임포트되는 테스트/스크립트에서도 항상 .env 값을 읽을 수 있도록 독립적으로도
# 호출해둔다(dotenv는 이미 설정된 환경변수를 덮어쓰지 않으므로 중복 호출해도 안전).
load_dotenv()

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

_TAG_RE = re.compile(r'</?b>')
# 실무자가 물어볼 법한 '용어의 뜻'을 확인하는 목적이므로, 일반 웹문서보다
# 정의/설명 위주인 백과사전을 우선하고, 백과사전에 없는 신조어/구어체 표현은
# 웹문서 검색으로 보완한다.
_ENDPOINTS = ("encyc", "webkr")


def _strip_tags(text):
    """네이버 검색 API 응답의 title/description에는 검색어를 강조하는 <b> 태그가
    그대로 섞여 오므로(예: "<b>꽃중년</b>이란..."), 프롬프트에 넣기 전에 제거한다."""
    return _TAG_RE.sub('', text or '')


def is_configured():
    """API 키가 설정되어 있는지 여부. 호출부에서 불필요한 네트워크 시도를 건너뛸 때 사용."""
    return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)


def search_term_meaning(term, display=2, timeout=5):
    """
    네이버 검색 API(백과사전 + 웹문서)로 term의 뜻을 짧게 설명하는 텍스트를 만든다.
    실패(키 없음/네트워크 오류/결과 없음) 시 None을 반환하며, 이 경우 호출부는
    무리하게 추측하지 말고 실무자에게 되묻는 기존 동작을 그대로 유지해야 한다.
    """
    if not is_configured() or not term:
        return None

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    snippets = []
    for endpoint in _ENDPOINTS:
        try:
            res = requests.get(
                f"https://openapi.naver.com/v1/search/{endpoint}.json",
                headers=headers, params={"query": term, "display": display}, timeout=timeout,
            )
            if res.status_code != 200:
                continue
            for item in res.json().get("items", [])[:display]:
                title = _strip_tags(item.get("title", ""))
                desc = _strip_tags(item.get("description", ""))
                if desc:
                    snippets.append(f"- {title}: {desc}" if title else f"- {desc}")
        except requests.exceptions.RequestException:
            continue
        except (ValueError, KeyError):
            # 응답이 JSON이 아니거나 예상 구조가 아닌 경우 - 이 엔드포인트만 건너뜀
            continue

        if len(snippets) >= display:
            break

    if not snippets:
        return None
    return "\n".join(snippets[:display])
