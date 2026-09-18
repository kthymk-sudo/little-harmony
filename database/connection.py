# database/connection.py
# ============================================================
# 🌟 [모듈화] 예전에는 database/db_manager.py 하나에 DB 연결부터 대화 저장까지
# 20개 가까운 함수가 전부 들어있었다. 파일 하나가 너무 길어지면 한 군데 실수가
# 전체에 영향을 줄 위험이 커진다는 판단 하에, 다른 모든 database 모듈이 공통으로
# 쓰는 저수준 기능(DB 연결, 데이터 버전 확인)만 이 파일로 분리했다.
# ============================================================
import os
import sqlite3
import glob
from config import DB_PATH


def _connect(db_file=DB_PATH):
    conn = sqlite3.connect(db_file, timeout=20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def get_data_version():
    """
    🌟 [버그 수정] 기존에는 적재된 DB 파일이 하나도 없을 때(예: 배포 직후,
    데이터를 한 번도 업로드하지 않은 상태) max()가 빈 시퀀스에 대해 ValueError를
    던져 앱이 그대로 죽었다. 파일이 하나도 없는 경우 0을 반환하도록 수정.
    """
    try:
        files = [DB_PATH] + glob.glob("harmony_history_*.db")
        mtimes = [os.path.getmtime(f) for f in files if os.path.exists(f)]
        return max(mtimes) if mtimes else 0
    except OSError:
        return 0


def get_loaded_periods():
    """
    🌟 [시스템 정보 - 월별 적재 현황] 시청내역 DB 자체가 harmony_history_YYYY_MM.db
    파일로 월별 분리되어 있으므로, 지금 존재하는 파일 목록만 보면 어느 달 데이터가
    들어있는지 바로 알 수 있다. 각 파일을 열어 실제 건수도 함께 세어준다.
    """
    periods = []
    for db_file in glob.glob("harmony_history_*.db"):
        base = os.path.basename(db_file)
        # "harmony_history_2026_08.db" -> "2026_08" -> "2026-08"
        month_label = base.replace("harmony_history_", "").replace(".db", "").replace("_", "-")

        row_count = 0
        try:
            conn = _connect(db_file)
            cur = conn.cursor()
            cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name='tb_history'")
            if cur.fetchone()[0] > 0:
                cur.execute('SELECT COUNT(*) FROM tb_history')
                row_count = cur.fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass

        periods.append({'month': month_label, 'rows': row_count})

    periods.sort(key=lambda p: p['month'])
    return periods
