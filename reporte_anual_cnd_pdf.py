import os
import io
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Image,
    Paragraph,
    Spacer,
    KeepTogether,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from db_mer import ejecutar_consulta_df

MESES_CORTOS_ES = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic"
]

CONDICION_FLEX_SQL = """
    UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
    AND REPLACE(LOWER(TRIM(COALESCE(tipo_oferta, ''))), '.', '') LIKE '%oi flexibilidad%'
    AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
"""


def construir_df_anual_mer(conn, anio, pais_filtro=None, mes_hasta=12):
    """Construye el resumen mensualizado (ene-dic) cortado hasta el mes indicado (mes_hasta)."""
    f_ini = f"{int(anio):04d}-01-01"
    f_fin = f"{int(anio):04d}-12-31"
    mes_limite = max(1, min(12, int(mes_hasta)))

    params = [f_ini, f_fin]
    cond_pais = ""
    if pais_filtro:
        cond_pais = " AND pais = ?"
        params.append(pais_filtro)

    df_tcp = ejecutar_consulta_df(
        f"""
        SELECT SUBSTR(CAST(fecha_hora AS TEXT), 6, 2) AS mes,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND NOT (LOWER(TRIM(tipo_transaccion)) = 'i' AND ({CONDICION_FLEX_SQL}))
        GROUP BY SUBSTR(CAST(fecha_hora AS TEXT), 6, 2), LOWER(TRIM(tipo_transaccion))
        """,
        conn,
        params=params,
    )

    df_top = ejecutar_consulta_df(
        f"""
        SELECT SUBSTR(CAST(fecha_hora AS TEXT), 6, 2) AS mes,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_oportunidad_top
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY SUBSTR(CAST(fecha_hora AS TEXT), 6, 2), LOWER(TRIM(tipo_transaccion))
        """,
        conn,
        params=params,
    )

    df_flex = ejecutar_consulta_df(
        f"""
        SELECT SUBSTR(CAST(fecha_hora AS TEXT), 6, 2) AS mes,
               SUM(ABS(mw_predespachados)) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND {CONDICION_FLEX_SQL}
        GROUP BY SUBSTR(CAST(fecha_hora AS TEXT), 6, 2)
        """,
        conn,
        params=params,
    )

    filas = []
    for m_idx in range(1, 13):
        mes_str = f"{m_idx:02d}"
        mes_etq = MESES_CORTOS_ES[m_idx - 1]

        if m_idx > mes_limite:
            filas.append({
                "mes_num": m_idx,
                "Fecha": mes_etq,
                "Compras_Contratos": None,
                "Compras_Oportunidad": None,
                "Ventas_Contratos": None,
                "Ventas_Oportunidad": None,
                "Total_Compras": None,
                "Total_Ventas": None,
            })
            continue

        def _val(df, tipo):
            if df.empty or "mes" not in df.columns:
                return None
            sub = df[(df["mes"].astype(str).str.strip().str.zfill(2) == mes_str) & (df["tipo"] == tipo)]
            if sub.empty or pd.isna(sub["mwh"].iloc[0]):
                return None
            return float(sub["mwh"].iloc[0])

        def _val_flex():
            if df_flex.empty or "mes" not in df_flex.columns:
                return 0.0
            sub = df_flex[df_flex["mes"].astype(str).str.strip().str.zfill(2) == mes_str]
            if sub.empty or pd.isna(sub["mwh"].iloc[0]):
                return 0.0
            return float(sub["mwh"].iloc[0])

        comp_tcp = _val(df_tcp, "r")
        comp_top = _val(df_top, "r")
        vent_tcp = _val(df_tcp, "i")
        vent_top_base = _val(df_top, "i")
        flex_mwh = _val_flex()

        if vent_top_base is not None or flex_mwh > 0:
            vent_top = (vent_top_base or 0.0) + flex_mwh
        else:
            vent_top = None

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


def _crear_grafica_barras_cnd(df_anual, anio, modo="compras"):
    """Genera imagen PNG estilo oficial CND: barras azul marino con etiquetas numéricas encima y borde gris."""
    meses = df_anual["Fecha"].tolist()
    x = list(range(len(meses)))

    if modo == "compras":
        valores = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Total_Compras"]]
        titulo = f"Retiros de energía del MER {anio} (MWh)"
    else:
        valores = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Total_Ventas"]]
        titulo = f"Inyecciones de energía al MER {anio} (MWh)"

    color_barra = "#1F4E78"
    fig, ax = plt.subplots(figsize=(8.6, 2.75), dpi=220)
    fig.patch.set_facecolor("white")
    fig.patch.set_edgecolor("#D9D9D9")
    fig.patch.set_linewidth(0.9)

    barras = ax.bar(x, valores, width=0.35, color=color_barra, edgecolor="none", zorder=3)

    max_val = max(valores, default=0.0)
    techo = max(max_val * 1.24, 100.0)
    ax.set_ylim(0, techo)

    ax.set_title(titulo, fontsize=11, fontweight="normal", color="#595959", pad=12)
    ax.set_xticks(x)
    ax.set_xticklabels(meses, fontsize=8, color="#595959")
    ax.tick_params(axis="y", labelsize=7.5, colors="#595959", length=0)
    ax.tick_params(axis="x", length=0)

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda val, pos: f"{val:,.0f}"))
    ax.grid(axis="y", linestyle="-", linewidth=0.5, color="#E0E0E0", zorder=0)

    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#BFBFBF")
    ax.spines["bottom"].set_linewidth(0.7)

    offset_y = techo * 0.025
    for rect, val in zip(barras, valores):
        etiqueta = f"{round(val):,}" if val > 0.4 else "0"
        y_pos = rect.get_height() + offset_y
        ax.text(
            rect.get_x() + rect.get_width() / 2.0,
            y_pos,
            etiqueta,
            ha="center",
            va="bottom",
            fontsize=6.8,
            color="white",
            fontweight="normal",
            bbox=dict(
                boxstyle="square,pad=0.22",
                facecolor=color_barra,
                edgecolor="none",
            ),
            zorder=4,
        )

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.12, dpi=220)
    plt.close(fig)
    buf.seek(0)
    return buf


def generar_pdf_transacciones_anuales_mer(
    conn,
    anio,
    pais_filtro=None,
    mes_hasta=12,
    ruta_logo="logo_cnd.png",
    ruta_salida=None,
):
    """Genera el PDF anual oficial 'TRANSACCIONES DE ENERGÍA EN EL MERCADO ELÉCTRICO REGIONAL'."""
    mes_limite = max(1, min(12, int(mes_hasta)))
    df_anual = construir_df_anual_mer(conn, anio, pais_filtro=pais_filtro, mes_hasta=mes_limite)

    etq_area = pais_filtro if pais_filtro else "REGIONAL"
    etq_mes = MESES_CORTOS_ES[mes_limite - 1]
    if not ruta_salida:
        ruta_salida = f"Transacciones_Energia_MER_{anio}_{etq_area}_hasta_{etq_mes}.pdf"

    def _fmt_celda(val, guion_si_cero=True):
        if val is None or pd.isna(val):
            return ""
        v = float(val)
        if abs(v) < 0.0005:
            return "-" if guion_si_cero else ""
        return f"{v:,.3f}"

    st_encabezado_inst = ParagraphStyle(
        "EncabezadoInstCND",
        fontName="Times-Bold",
        fontSize=12.5,
        leading=14.5,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    st_titulo_tabla = ParagraphStyle(
        "TituloTablaMER",
        fontName="Helvetica-Bold",
        fontSize=9.2,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.white,
    )
    st_hdr = ParagraphStyle(
        "HdrTablaAnual",
        fontName="Helvetica-Bold",
        fontSize=8.0,
        leading=9.5,
        alignment=TA_CENTER,
        textColor=colors.white,
    )

    if os.path.exists(ruta_logo):
        logo_elem = Image(ruta_logo, width=2.2 * cm, height=1.55 * cm)
    else:
        logo_elem = Paragraph("", st_encabezado_inst)

    encabezado_doc = Table(
        [[logo_elem, Paragraph("CENTRO NACIONAL<br/>DE DESPACHO", st_encabezado_inst)]],
        colWidths=[2.5 * cm, 15.7 * cm],
    )
    encabezado_doc.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEAFTER", (0, 0), (0, 0), 1.3, colors.black),
    ]))

    filas_tabla = [
        [Paragraph(f"TRANSACCIONES DE ENERGÍA EN EL MERCADO ELÉCTRICO REGIONAL {anio}", st_titulo_tabla), "", "", "", "", "", ""],
        [
            Paragraph("Fecha", st_hdr),
            Paragraph("COMPRAS DE ENERGÍA", st_hdr),
            "",
            Paragraph("VENTAS DE ENERGÍA", st_hdr),
            "",
            Paragraph("TOTAL", st_hdr),
            "",
        ],
        [
            "",
            Paragraph("CONTRATOS", st_hdr),
            Paragraph("OPORTUNIDAD", st_hdr),
            Paragraph("CONTRATOS", st_hdr),
            Paragraph("OPORTUNIDAD", st_hdr),
            Paragraph("COMPRAS", st_hdr),
            Paragraph("VENTAS", st_hdr),
        ],
        [
            "",
            Paragraph("MWh", st_hdr),
            Paragraph("MWh", st_hdr),
            Paragraph("MWh", st_hdr),
            Paragraph("MWh", st_hdr),
            Paragraph("MWh", st_hdr),
            Paragraph("MWh", st_hdr),
        ],
    ]

    for _, r in df_anual.iterrows():
        m_num = int(r["mes_num"])
        if m_num > mes_limite:
            filas_tabla.append([r["Fecha"], "", "", "", "", "-", "-"])
        else:
            filas_tabla.append([
                r["Fecha"],
                _fmt_celda(r["Compras_Contratos"], guion_si_cero=True),
                _fmt_celda(r["Compras_Oportunidad"], guion_si_cero=True),
                _fmt_celda(r["Ventas_Contratos"], guion_si_cero=True),
                _fmt_celda(r["Ventas_Oportunidad"], guion_si_cero=True),
                _fmt_celda(r["Total_Compras"], guion_si_cero=True),
                _fmt_celda(r["Total_Ventas"], guion_si_cero=True),
            ])

    tot_cc = float(df_anual["Compras_Contratos"].dropna().sum())
    tot_co = float(df_anual["Compras_Oportunidad"].dropna().sum())
    tot_vc = float(df_anual["Ventas_Contratos"].dropna().sum())
    tot_vo = float(df_anual["Ventas_Oportunidad"].dropna().sum())
    tot_tc = float(df_anual["Total_Compras"].dropna().sum())
    tot_tv = float(df_anual["Total_Ventas"].dropna().sum())

    filas_tabla.append([
        "Total",
        _fmt_celda(tot_cc, guion_si_cero=True),
        _fmt_celda(tot_co, guion_si_cero=True),
        _fmt_celda(tot_vc, guion_si_cero=True),
        _fmt_celda(tot_vo, guion_si_cero=True),
        _fmt_celda(tot_tc, guion_si_cero=True),
        _fmt_celda(tot_tv, guion_si_cero=True),
    ])

    color_azul_cnd = colors.HexColor("#16365C")
    ancho_cols = [1.8 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm, 2.7 * cm, 2.7 * cm]
    tabla = Table(filas_tabla, colWidths=ancho_cols, repeatRows=4)

    tabla.setStyle(TableStyle([
        ("SPAN", (0, 0), (6, 0)),
        ("SPAN", (0, 1), (0, 3)),
        ("SPAN", (1, 1), (2, 1)),
        ("SPAN", (3, 1), (4, 1)),
        ("SPAN", (5, 1), (6, 1)),

        ("BACKGROUND", (0, 0), (-1, 3), color_azul_cnd),
        ("ALIGN", (0, 0), (-1, 3), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 3), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, 3), 3.5),

        ("FONTNAME", (0, 4), (-1, -2), "Helvetica"),
        ("FONTSIZE", (0, 4), (-1, -2), 8.2),
        ("ALIGN", (0, 4), (0, -1), "CENTER"),
        ("ALIGN", (1, 4), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 4), (-1, -2), 2.5),
        ("BOTTOMPADDING", (0, 4), (-1, -2), 2.5),

        ("BACKGROUND", (0, -1), (-1, -1), color_azul_cnd),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 8.5),
        ("TOPPADDING", (0, -1), (-1, -1), 3.8),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 3.8),

        ("BOX", (0, 0), (-1, -1), 1.3, colors.black),
        ("LINEBELOW", (0, 3), (-1, 3), 1.1, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 1.1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))

    buf_retiros = _crear_grafica_barras_cnd(df_anual, anio, modo="compras")
    buf_inyecciones = _crear_grafica_barras_cnd(df_anual, anio, modo="ventas")

    img_retiros = Image(buf_retiros, width=18.0 * cm, height=5.5 * cm)
    img_inyecciones = Image(buf_inyecciones, width=18.0 * cm, height=5.5 * cm)

    doc = SimpleDocTemplate(
        ruta_salida,
        pagesize=letter,
        topMargin=0.8 * cm,
        bottomMargin=0.8 * cm,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
    )

    elementos = [
        encabezado_doc,
        Spacer(1, 0.28 * cm),
        tabla,
        Spacer(1, 0.32 * cm),
        KeepTogether([img_retiros, Spacer(1, 0.22 * cm), img_inyecciones]),
    ]
    doc.build(elementos)
    return ruta_salida
