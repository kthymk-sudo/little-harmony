# ui/chart_render.py
# ============================================================
# 🌟 [모듈화] chart_spec(dict, JSON 직렬화 가능한 순수 피벗 데이터)을 plotly
# figure/표 DataFrame으로 되돌리는 공용 렌더링 로직. 원래 ui/analysis_chat.py에만
# 있었는데, 📝 보고서 탭도 같은 피벗 엔진(services/analysis_service.run_pivot_analysis)
# 결과를 그대로 보여줘야 해서 공용 모듈로 뽑았다.
#
# 🌟 [다중 행/열/값] 행이 2개면 가로축 라벨을 "채널A · 여자"처럼 합치고, 측정값이 여러
# 개면 단위가 달라도 각각 잘 보이도록 측정값마다 그래프를 위아래로 나눠 그린다(가로축 공유).
# 같은 열 값(예: 여자)은 모든 그래프에서 같은 색으로 맞춘다.
# ============================================================
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from services.analysis_service import normalize_pivot_result, describe_pivot

_BAR_LABEL_MAX_ROWS = 40  # 막대가 이보다 많으면 막대 위 숫자는 생략(빽빽해짐)


def pivot_table_df(pivot_result):
    """chart_spec의 pivot 결과(dict)를 화면 표시용 DataFrame으로 되돌린다."""
    data = normalize_pivot_result(pivot_result or {})
    rows = data.get("결과") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    series_cols = [c for cols in (data.get("계열") or {}).values() for c in cols]
    ordered_cols = [c for c in (data.get("행컬럼") or []) + series_cols if c in df.columns]
    return df[ordered_cols] if ordered_cols else df


def build_pivot_chart_figure(chart_spec):
    """chart_spec(dict)에서 plotly figure를 만든다. 저장/복원 시에는 이 dict만
    오가고, figure 자체는 매번 이 함수로 다시 만든다."""
    if not chart_spec or chart_spec.get("type") != "pivot":
        return None
    data = normalize_pivot_result(chart_spec.get("data") or {})
    table_df = pivot_table_df(data)
    if table_df is None or table_df.empty:
        return None

    row_cols = data.get("행컬럼") or []
    x = table_df[row_cols].astype(str).agg(' · '.join, axis=1) if len(row_cols) > 1 else table_df[row_cols[0]].astype(str)
    measures = list((data.get("계열") or {}).keys())
    units = data.get("단위") or {}
    labels = data.get("계열라벨") or {}
    is_line = data.get("차트유형") == "선"

    n = len(measures)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=True, vertical_spacing=0.12 if n > 1 else 0.02,
        subplot_titles=[f"{m}({units.get(m, '')})" for m in measures] if n > 1 else None,
    )
    palette = px.colors.qualitative.Plotly
    colors, shown = {}, set()
    for i, m in enumerate(measures, start=1):
        series = data["계열"][m]
        for s in series:
            label = labels.get(s, s)
            color = colors.setdefault(label, palette[len(colors) % len(palette)])
            common = dict(x=x, y=table_df[s], name=label, legendgroup=label,
                          showlegend=len(series) > 1 and label not in shown)
            shown.add(label)
            if is_line:
                fig.add_trace(go.Scatter(mode='lines+markers', line=dict(color=color), **common), row=i, col=1)
            else:
                show_text = len(series) == 1 and len(table_df) <= _BAR_LABEL_MAX_ROWS
                fig.add_trace(go.Bar(
                    marker_color=color, text=table_df[s] if show_text else None,
                    texttemplate='%{y:,}' if show_text else None, textposition='outside' if show_text else None,
                    **common,
                ), row=i, col=1)
        fig.update_yaxes(title_text=f"{m}({units.get(m, '')})" if n == 1 else None, row=i, col=1)

    title = describe_pivot(data)
    if data.get('데이터기준'):  # 시청이력 기준인지 누적 통계 기준인지 차트에서 바로 보이게
        title += f"<br><sup>{data['데이터기준']}</sup>"
    fig.update_xaxes(categoryorder='array', categoryarray=list(x))
    fig.update_xaxes(title_text=' · '.join(data.get('행') or []), row=n, col=1)
    fig.update_layout(
        title=title, barmode='group', height=max(420, 300 * n + 120), margin=dict(t=90, b=10),
        legend_title_text=' · '.join(data.get('열') or []) or None,
    )
    return fig
