import os
import math
import pandas as pd

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    Image as RLImage,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.charts.barcharts import VerticalBarChart

from db_mer import ejecutar_consulta_df

MESES_CORTOS_ES = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
]


def construir_df_anual_mer(conn, anio, pais_filtro=None):
    """Construye el resumen mensualizado (ene-dic) de compras y ventas (TCP y TOP) para un año."""
    f_ini = f"{int(anio):04d}-01-01"
    f_fin = f"{int(anio):04d}-12-31"

    params = [f_ini, f_fin]
    cond_pais = ""
    if pais_filtro:
        cond_pais = " AND pais = ?"
        params.append(pais_filtro)

    df_tcp = ejecutar_consulta_df(
        f"""
        SELECT strftime('%m', fecha_hora) AS mes,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY strftime('%m', fecha_hora), LOWER(TRIM(tipo_transaccion))
        """,
        conn,
        params=params,
    )

    df_top = ejecutar_consulta_df(
        f"""
        SELECT strftime('%m', fecha_hora) AS mes,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_oportunidad_top
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY strftime('%m', fecha_hora), LOWER(TRIM(tipo_transaccion))
        """,
        conn,
        params=params,
    )

    filas = []
    for m_idx in range(1, 13):
        mes_str = f"{m_idx:02d}"
        mes_etq = MESES_CORTOS_ES[m_idx - 1]

        def _val(df, tipo):
            if df.empty or "mes" not in df.columns:
                return None
            sub = df[(df["mes"].astype(str).str.zfill(2) == mes_str) & (df["tipo"] == tipo)]
            if sub.empty or pd.isna(sub["mwh"].iloc[0]):
                return None
            return float(sub["mwh"].iloc[0])

        comp_tcp = _val(df_tcp, "r")
        comp_top = _val(df_top, "r")
        vent_tcp = _val(df_tcp, "i")
        vent_top = _val(df_top, "i")

        tiene_compras = (comp_tcp is not None) or (comp_top is not None)
        tiene_ventas = (vent_tcp is not None) or (vent_top is not None)

        tot_compras = ((comp_tcp or 0.0) + (comp_top or 0.0)) if tiene_compras else None
        tot_ventas = ((vent_tcp or 0.0) + (vent_top or 0.0)) if tiene_ventas else None

        filas.append({
            "mes_num": m_idx,
            "Fecha": mes_etq,
            "Compras_Contratos": comp_tcp,
            "Compras_Oportunidad": comp_top,
            "Ventas_Contratos": vent_tcp,
            "Ventas_Oportunidad": vent_top,
            "Total_Compras": tot_compras,
            "Total_Ventas": tot_ventas,
        })

    return pd.DataFrame(filas)


def _construir_logo_cnd(ruta_logo, max_w, max_h):
    if ruta_logo and os.path.exists(ruta_logo):
        try:
            lector = ImageReader(ruta_logo)
            iw, ih = lector.getSize()
            if iw > 0 and ih > 0:
                escala = min(max_w / float(iw), max_h / float(ih))
                return RLImage(ruta_logo, width=iw * escala, height=ih * escala)
        except Exception:
            pass
    return Spacer(1, 1)


def _crear_grafico_barras_mer(titulo, valores, ancho_pt=525, alto_pt=152):
    """Dibuja un gráfico de barras estilo institucional con etiquetas de datos sobre cada barra."""
    azul_cnd = colors.HexColor("#1F4E78")
    borde_caja = colors.HexColor("#D9D9D9")
    rejilla = colors.HexColor("#E6E6E6")
    texto_gris = colors.HexColor("#595959")

    d = Drawing(ancho_pt, alto_pt)
    d.add(Rect(0, 0, ancho_pt, alto_pt, strokeColor=borde_caja, fillColor=colors.white, strokeWidth=0.8))

    d.add(
        String(
            ancho_pt / 2.0,
            alto_pt - 16,
            titulo,
            fontName="Helvetica-Bold",
            fontSize=9.5,
            fillColor=texto_gris,
            textAnchor="middle",
        )
    )

    vals_clean = [max(0.0, float(v or 0.0)) for v in valores]
    max_val = max(vals_clean) if vals_clean else 0.0

    if max_val <= 0:
        y_max = 100.0
        y_step = 20.0
    else:
        orden = 10 ** math.floor(math.log10(max_val))
        norm = max_val / orden
        if norm <= 1.0:
            top_norm, step_norm = 1.2, 0.2
        elif norm <= 2.5:
            top_norm, step_norm = 3.0, 0.5
        elif norm <= 4.5:
            top_norm, step_norm = 5.0, 0.5
        elif norm <= 9.0:
            top_norm, step_norm = 10.0, 1.0
        else:
            top_norm, step_norm = 12.0, 2.0
        y_max = top_norm * orden
        y_step = step_norm * orden

    bc = VerticalBarChart()
    bc.x = 46
    bc.y = 22
    bc.width = ancho_pt - 62
    bc.height = alto_pt - 50
    bc.data = [vals_clean]
    bc.strokeColor = None
    bc.barWidth = 14
    bc.groupSpacing = 12

    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = y_max
    bc.valueAxis.valueStep = y_step
    bc.valueAxis.strokeColor = rejilla
    bc.valueAxis.gridStrokeColor = rejilla
    bc.valueAxis.gridStrokeWidth = 0.5
    bc.valueAxis.visibleGrid = True
    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 6.8
    bc.valueAxis.labels.fillColor = texto_gris
    bc.valueAxis.labelTextFormat = lambda v: f"{int(round(v)):,}"

    bc.categoryAxis.categoryNames = MESES_CORTOS_ES
    bc.categoryAxis.strokeColor = rejilla
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 7.2
    bc.categoryAxis.labels.fillColor = texto_gris
    bc.categoryAxis.labels.boxAnchor = "n"
    bc.categoryAxis.labels.dy = -3

    bc.bars[0].fillColor = azul_cnd
    bc.bars[0].strokeColor = azul_cnd

    bc.barLabels.nudge = 7
    bc.barLabelFormat = lambda v: f"{int(round(v)):,}"
    bc.barLabels.fontName = "Helvetica-Bold"
    bc.barLabels.fontSize = 6.5
    bc.barLabels.fillColor = colors.white
    bc.barLabels.boxFillColor = azul_cnd
    bc.barLabels.boxStrokeColor = azul_cnd
    bc.barLabels.boxAnchor = "c"
    bc.barLabels.topPadding = 1.5
    bc.barLabels.bottomPadding = 1.5
    bc.barLabels.leftPadding = 2.5
    bc.barLabels.rightPadding = 2.5

    d.add(bc)
    return d


def generar_pdf_transacciones_anuales_mer(
    conn,
    anio,
    pais_filtro=None,
    ruta_logo="logo_cnd.png",
):
    """Genera el PDF anual 'Transacciones de Energía en el Mercado Eléctrico Regional' con tabla y 2 gráficas."""
    anio_int = int(anio)
    df_anual = construir_df_anual_mer(conn, anio_int, pais_filtro)

    sufijo_pais = f"_{pais_filtro}" if pais_filtro else ""
    nombre_archivo = f"Transacciones_Energia_MER_{anio_int}{sufijo_pais}.pdf"

    doc = SimpleDocTemplate(
        nombre_archivo,
        pagesize=letter,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.40 * inch,
    )

    elementos = []
    styles = getSampleStyleSheet()
    azul_cnd = colors.HexColor("#1F4E78")

    estilo_cnd = ParagraphStyle(
        "TituloCNDAnual",
        parent=styles["Normal"],
        fontName="Times-Bold",
        fontSize=13,
        leading=15,
        textColor=colors.HexColor("#1A1A1A"),
        alignment=TA_LEFT,
    )

    logo_img = _construir_logo_cnd(ruta_logo, max_w=1.25 * inch, max_h=0.68 * inch)
    encabezado = Table(
        [[logo_img, Paragraph("CENTRO NACIONAL<br/>DE DESPACHO", estilo_cnd)]],
        colWidths=[1.35 * inch, 5.95 * inch],
    )
    encabezado.setStyle(
        TableStyle([
            ("LINEAFTER", (0, 0), (0, 0), 1.4, colors.HexColor("#262626")),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 10),
            ("LEFTPADDING", (1, 0), (1, 0), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    elementos.append(encabezado)
    elementos.append(Spacer(1, 0.14 * inch))

    titulo_tabla = f"TRANSACCIONES DE ENERGÍA EN EL MERCADO ELÉCTRICO REGIONAL {anio_int}"
    if pais_filtro:
        titulo_tabla += f" ({pais_filtro})"

    fila_h0 = [titulo_tabla, "", "", "", "", "", ""]
    fila_h1 = ["Fecha", "COMPRAS DE ENERGÍA", "", "VENTAS DE ENERGÍA", "", "TOTAL", ""]
    fila_h2 = ["", "CONTRATOS", "OPORTUNIDAD", "CONTRATOS", "OPORTUNIDAD", "COMPRAS", "VENTAS"]
    fila_h3 = ["", "MWh", "MWh", "MWh", "MWh", "MWh", "MWh"]

    def _fmt_celda(val, es_total_col=False):
        if val is None or pd.isna(val):
            return "-" if es_total_col else ""
        if abs(float(val)) < 0.0005:
            return "-"
        return f"{float(val):,.3f}"

    filas_meses = []
    for _, r in df_anual.iterrows():
        filas_meses.append([
            r["Fecha"],
            _fmt_celda(r["Compras_Contratos"], False),
            _fmt_celda(r["Compras_Oportunidad"], False),
            _fmt_celda(r["Ventas_Contratos"], False),
            _fmt_celda(r["Ventas_Oportunidad"], False),
            _fmt_celda(r["Total_Compras"], True),
            _fmt_celda(r["Total_Ventas"], True),
        ])

    def _sum_col(col_name):
        s = df_anual[col_name].dropna().sum()
        return "-" if abs(float(s)) < 0.0005 else f"{float(s):,.3f}"

    fila_total = [
        "Total",
        _sum_col("Compras_Contratos"),
        _sum_col("Compras_Oportunidad"),
        _sum_col("Ventas_Contratos"),
        _sum_col("Ventas_Oportunidad"),
        _sum_col("Total_Compras"),
        _sum_col("Total_Ventas"),
    ]

    datos_tabla = [fila_h0, fila_h1, fila_h2, fila_h3] + filas_meses + [fila_total]
    idx_tot = len(datos_tabla) - 1

    anchos = [0.75 * inch, 1.12 * inch, 1.12 * inch, 1.05 * inch, 1.12 * inch, 1.12 * inch, 1.02 * inch]
    tabla = Table(datos_tabla, colWidths=anchos)
    tabla.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
            ("BOX", (0, 0), (-1, -1), 1.3, colors.black),
            ("SPAN", (0, 0), (6, 0)),
            ("SPAN", (0, 1), (0, 3)),
            ("SPAN", (1, 1), (2, 1)),
            ("SPAN", (3, 1), (4, 1)),
            ("SPAN", (5, 1), (6, 1)),
            ("BACKGROUND", (0, 0), (-1, 3), azul_cnd),
            ("TEXTCOLOR", (0, 0), (-1, 3), colors.white),
            ("FONTNAME", (0, 0), (-1, 3), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (6, 0), 8.5),
            ("FONTSIZE", (0, 1), (-1, 3), 7.8),
            ("ALIGN", (0, 0), (-1, 3), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 4), (-1, idx_tot - 1), "Helvetica"),
            ("FONTSIZE", (0, 4), (-1, idx_tot - 1), 7.8),
            ("ALIGN", (0, 4), (0, idx_tot - 1), "CENTER"),
            ("ALIGN", (1, 4), (-1, idx_tot - 1), "RIGHT"),
            ("BACKGROUND", (0, idx_tot), (-1, idx_tot), azul_cnd),
            ("TEXTCOLOR", (0, idx_tot), (-1, idx_tot), colors.white),
            ("FONTNAME", (0, idx_tot), (-1, idx_tot), "Helvetica-Bold"),
            ("FONTSIZE", (0, idx_tot), (-1, idx_tot), 8.0),
            ("ALIGN", (0, idx_tot), (0, idx_tot), "CENTER"),
            ("ALIGN", (1, idx_tot), (-1, idx_tot), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.1),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ])
    )
    elementos.append(tabla)
    elementos.append(Spacer(1, 0.14 * inch))

    vals_retiros = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Total_Compras"]]
    graf_retiros = _crear_grafico_barras_mer(
        titulo=f"Retiros de energía del MER {anio_int} (MWh)",
        valores=vals_retiros,
        ancho_pt=7.3 * inch,
        alto_pt=2.12 * inch,
    )
    elementos.append(graf_retiros)
    elementos.append(Spacer(1, 0.12 * inch))

    vals_inyecciones = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Total_Ventas"]]
    graf_inyecciones = _crear_grafico_barras_mer(
        titulo=f"Inyecciones de energía al MER {anio_int} (MWh)",
        valores=vals_inyecciones,
        ancho_pt=7.3 * inch,
        alto_pt=2.12 * inch,
    )
    elementos.append(graf_inyecciones)

    doc.build(elementos)
    return nombre_archivo
