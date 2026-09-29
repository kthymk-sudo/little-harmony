# ui/chart_render.py
# ============================================================
# 🌟 [모듈화] chart_spec(dict, JSON 직렬화 가능한 순수 피벗 데이터)을 plotly
# figure/표 DataFrame으로 되돌리는 공용 렌더링 로직. 원래 ui/analysis_chat.py에만
# 있었는데, 📝 보고서 탭도 같은 피벗 엔진(services/analysis_service.run_pivot_analysis)
# 결과를 그대로 보여줘야 해서 공용 모듈로 뽑았다.
# ============================================================
import pandas as pd
import plotly.express as px


def pivot_table_df(pivot_result):
    """chart_spec의 pivot 결과(dict)를 화면 표시용 DataFrame으로 되돌린다."""
    rows = pivot_result.get("결과") or []
    if not rows:
        return None
    row_col = pivot_result.get("행컬럼")
    series_cols = pivot_result.get("계열컬럼") or []
    df = pd.DataFrame(rows)
    ordered_cols = [c for c in [row_col] + series_cols if c in df.columns]
    return df[ordered_cols] if ordered_cols else df


def build_pivot_chart_figure(chart_spec):
    """chart_spec(dict)에서 plotly figure를 만든다. 저장/복원 시에는 이 dict만
    오가고, figure 자체는 매번 이 함수로 다시 만든다."""
    if not chart_spec or chart_spec.get("type") != "pivot":
        return None
    data = chart_spec.get("data") or {}
    table_df = pivot_table_df(data)
    if table_df is None or table_df.empty:
        return None

    row_col = data.get("행컬럼")
    series_cols = data.get("계열컬럼") or []
    unit = data.get("단위", "")
    y_label = f"{data.get('측정값', '')}({unit})" if unit else data.get('측정값', '')
    title = f"{data.get('행', '')}별 {data.get('측정값', '')}" + (f" / {data.get('열')} 비교" if data.get('열') else "")

    is_line = (data.get("차트유형") == "선")
    chart_fn = px.line if is_line else px.bar
    if len(series_cols) <= 1:
        y_col = series_cols[0] if series_cols else None
        extra_kwargs = {"markers": True} if is_line else {"text": y_col}
        fig = chart_fn(table_df, x=row_col, y=y_col, **extra_kwargs)
    else:
        melted = table_df.melt(id_vars=[row_col], value_vars=series_cols, var_name=data.get('열') or '구분', value_name=y_label)
        color_col = data.get('열') or '구분'
        if chart_fn is px.line:
            fig = px.line(melted, x=row_col, y=y_label, color=color_col, markers=True)
        else:
            fig = px.bar(melted, x=row_col, y=y_label, color=color_col, barmode='group')

    if chart_fn is px.bar:
        fig.update_traces(texttemplate='%{y:,.1f}', textposition='outside')
    fig.update_layout(title=title, yaxis_title=y_label, xaxis_title=data.get('행', ''), margin=dict(t=50, b=10))
    return fig
