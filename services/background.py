# services/background.py
# ============================================================
# 🌟 [백그라운드 작업] AI 답변을 만드는 동안 사용자가 다른 탭·대화로 넘어가면 Streamlit이 화면 스크립트를 중단해서 답변
# 만들기도 같이 끊겼다. 그래서 답변 만들기(AI 호출·계산)를 별도 스레드에서 돌리고, 끝나면 그 스레드가 대화를 DB에 저장한다.
# 화면은 나중에 돌아왔을 때 끝난 결과를 꺼내 보여주기만 한다. 대화(id)마다 진행 중인 작업은 하나다.
# 이 모듈은 Streamlit에 의존하지 않는다(작업 함수는 st.session_state를 건드리면 안 된다 - 필요한 값은 미리 복사해 넘긴다).
# ============================================================
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hp-job")
_jobs = {}
_lock = threading.Lock()
_KEEP_SECONDS = 1800   # 끝난 뒤 이만큼 안 꺼내가면 정리한다(메모리)


class Job:
    def __init__(self, label):
        self.label = label      # 진행 표시 기본 문구
        self.progress = ""      # 작업 함수가 갱신하는 세부 진행 문구(예: "2단계 계산 중 · ...")
        self.future = None
        self.finished_at = None

    def done(self):
        return self.future.done()


def submit(key, work, persist=None, label="", wrap=None):
    """key(대화 id)의 작업을 시작한다. work(job) -> 결과. 성공하면 같은 스레드에서 persist(결과)를 불러 DB에 저장한다
    (사용자가 다른 화면에 있어도 저장되도록). 이미 진행 중인 작업이 있으면 새로 시작하지 않고 그것을 돌려준다.
    wrap: 실행 함수를 감싸는 함수(Streamlit 스크립트 컨텍스트를 붙이는 등)."""
    job = Job(label)

    def run():
        try:
            result = work(job)
            if persist is not None:
                try:
                    persist(result)
                except Exception:   # 저장에 실패해도 결과는 화면에 전달한다(화면 쪽 자동저장이 한 번 더 시도한다)
                    pass
            return result
        finally:
            job.finished_at = time.time()

    with _lock:
        now = time.time()
        for k in [k for k, j in _jobs.items() if j.finished_at and now - j.finished_at > _KEEP_SECONDS]:
            del _jobs[k]
        current = _jobs.get(key)
        if current is not None and not current.done():
            return current
        job.future = _pool.submit(wrap(run) if wrap else run)
        _jobs[key] = job
    return job


def get(key):
    """이 대화의 작업(진행 중이거나 끝났지만 아직 안 꺼낸 것). 없으면 None."""
    with _lock:
        return _jobs.get(key)


def take(key):
    """끝난 작업을 꺼낸다: ('ok', 결과) 또는 ('error', 예외). 없거나 아직 진행 중이면 None."""
    with _lock:
        job = _jobs.get(key)
        if job is None or not job.done():
            return None
        del _jobs[key]
    try:
        return "ok", job.future.result()
    except Exception as e:
        return "error", e


if __name__ == "__main__":
    import threading as _t

    # 작업은 화면(스크립트)과 상관없이 끝까지 돈다: 결과는 저장되고, 나중에 꺼내면 그대로 있다
    saved, gate = [], _t.Event()

    def slow(job):
        job.progress = "1단계"
        gate.wait(5)
        return ["답변"]

    job = submit("대화1", slow, persist=saved.append, label="분석 중")
    assert get("대화1") is job and not job.done() and take("대화1") is None
    assert submit("대화1", slow) is job, "진행 중이면 새로 시작하지 않는다"
    gate.set()
    job.future.result(5)
    assert saved == [["답변"]] and job.progress == "1단계"
    assert take("대화1") == ("ok", ["답변"]) and get("대화1") is None and take("대화1") is None

    # 실패하면 예외를 돌려주고, 저장 실패는 결과를 막지 않는다
    def boom(job):
        raise RuntimeError("실패")

    submit("대화2", boom).future.exception(5)
    status, err = take("대화2")
    assert status == "error" and str(err) == "실패"

    def bad_persist(_):
        raise OSError("디스크")

    submit("대화3", lambda job: 1, persist=bad_persist).future.result(5)
    assert take("대화3") == ("ok", 1)

    # 오래 안 꺼낸 끝난 작업은 정리된다
    submit("대화4", lambda job: 1).future.result(5)
    _jobs["대화4"].finished_at = time.time() - _KEEP_SECONDS - 1
    submit("대화5", lambda job: 1).future.result(5)
    assert get("대화4") is None and get("대화5") is not None
    print("background self-check OK")
