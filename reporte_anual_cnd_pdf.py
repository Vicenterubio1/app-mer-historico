import os
import io
from datetime import datetime
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

    # Compras ('r') se mantienen íntegras; en Ventas ('i') de TCP se excluyen las flexibilizadas a Oportunidad
    df_tcp = ejecutar_consulta_df(
        f"""
        SELECT SUBSTR(CAST(fecha_hora AS TEXT), 6, 2) AS mes,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND NOT (
              LOWER(TRIM(tipo_transaccion)) = 'i'
              AND UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
              AND LOWER(TRIM(COALESCE(tipo_oferta, ''))) = 'o.i. flexibilidad'
              AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
          )
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

    # Ventas Oportunidad desde CNFFF + O.I. Flexibilidad en TCP
    df_flex = ejecutar_consulta_df(
        f"""
        SELECT SUBSTR(CAST(fecha_hora AS TEXT), 6, 2) AS mes,
               SUM(ABS(mw_predespachados)) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
          AND LOWER(TRIM(COALESCE(tipo_oferta, ''))) = 'o.i. flexibilidad'
          AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
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


def _crear_grafica_barras_apiladas(df_anual, modo="compras"):
    """Genera imagen PNG en memoria de barras apiladas estilo CND (Contratos vs Oportunidad)."""
    meses = df_anual["Fecha"].tolist()
    x = list(range(len(meses)))

    if modo == "compras":
        serie_cont = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Compras_Contratos"]]
        serie_opor = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Compras_Oportunidad"]]
        titulo = "RETIROS DE ENERGIA MER"
        lbl_cont = "RETIROS CONTRATOS"
        lbl_opor = "RETIROS OPORTUNIDAD"
        color_cont = "#2E6B9E"
        color_opor = "#4F9CD6"
    else:
        serie_cont = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Ventas_Contratos"]]
        serie_opor = [float(v) if pd.notna(v) else 0.0 for v in df_anual["Ventas_Oportunidad"]]
        titulo = "INYECCIONES DE ENERGIA MER"
        lbl_cont = "INYECCIONES CONTRATOS"
        lbl_opor = "INYECCIONES OPORTUNIDAD"
        color_cont = "#1B7A43"
        color_opor = "#48B872"

    fig, ax = plt.subplots(figsize=(8.6, 3.05), dpi=220)
    ancho = 0.56

    ax.bar(x, serie_cont, width=ancho, label=lbl_cont, color=color_cont, edgecolor="none", zorder=3)
    ax.bar(x, serie_opor, width=ancho, bottom=serie_cont, label=lbl_opor, color=color_opor, edgecolor="none", zorder=3)

    max_tot = max((c + o for c, o in zip(serie_cont, serie_opor)), default=0.0)
    techo = max(max_tot * 1.18, 100.0)
    ax.set_ylim(0, techo)

    ax.set_title(titulo, fontsize=10, fontweight="bold", color="#222222", pad=22)
    ax.set_ylabel("MWh", fontsize=8.5, fontweight="bold", color="#333333")
    ax.set_xticks(x)
    ax.set_xticklabels(meses, fontsize=8, color="#222222")
    ax.tick_params(axis="y", labelsize=7.5, colors="#333333")

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda val, pos: f"{val:,.0f}"))
    ax.grid(axis="y", linestyle="-", linewidth=0.5, color="#D9D9D9", zorder=0)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#888888")
    ax.spines["bottom"].set_color("#888888")

    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 1.13),
        ncol=2,
        frameon=False,
        fontsize=7.5,
        handlelength=1.1,
        handleheight=0.9,
    )

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=220)
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
    """Genera el PDF anual 'Transacciones de Energía en el MER' con tabla mensualizada y 2 gráficas."""
    mes_limite = max(1, min(12, int(mes_hasta)))
    df_anual = construir_df_anual_mer(conn, anio, pais_filtro=pais_filtro, mes_hasta=mes_limite)

    etq_area = pais_filtro if pais_filtro else "REGIONAL"
    etq_mes = MESES_CORTOS_ES[mes_limite - 1]
    if not ruta_salida:
        ruta_salida = f"Transacciones_Energia_MER_{anio}_{etq_area}_hasta_{etq_mes}.pdf"

    def _fmt_celda(val):
        if val is None or pd.isna(val):
            return ""
        v = float(val)
        if abs(v) < 0.0005:
            return "-"
        return f"{v:,.3f}"

    st_titulo_doc = ParagraphStyle(
        "TituloAnualCND",
        fontName="Helvetica-Bold",
        fontSize=12.5,
        leading=15,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#1B365D"),
    )
    st_subtitulo_doc = ParagraphStyle(
        "SubtituloAnualCND",
        fontName="Helvetica",
        fontSize=9,
        leading=11.5,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#444444"),
    )
    st_hdr = ParagraphStyle(
        "HdrTablaAnual",
        fontName="Helvetica-Bold",
        fontSize=7.4,
        leading=9.0,
        alignment=TA_CENTER,
        textColor=colors.white,
    )

    if os.path.exists(ruta_logo):
        logo_elem = Image(ruta_logo, width=3.5 * cm, height=1.25 * cm)
    else:
        logo_elem = Paragraph(
            "<b>CENTRO NACIONAL<br/>DE DESPACHO</b>",
            ParagraphStyle("LogoAnualFallback", fontName="Helvetica-Bold", fontSize=8.5, leading=10, alignment=TA_CENTER),
        )

    subtitulo_area = f"Área de Control: {pais_filtro}" if pais_filtro else "Mercado Eléctrico Regional (MER)"
    encabezado_doc = Table(
        [
            [
                logo_elem,
                [
                    Paragraph(f"Transacciones de Energía en el MER — Año {anio} (Enero – {etq_mes.capitalize()})", st_titulo_doc),
                    Paragraph(f"{subtitulo_area} · Cantidades expresadas en MWh", st_subtitulo_doc),
                ],
            ]
        ],
        colWidths=[4.0 * cm, 14.2 * cm],
    )
    encabezado_doc.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#1B365D")),
    ]))

    filas_tabla = [
        [
            Paragraph("Fecha", st_hdr),
            Paragraph("COMPRAS<br/>CONTRATOS<br/>(MWh)", st_hdr),
            Paragraph("COMPRAS<br/>OPORTUNIDAD<br/>(MWh)", st_hdr),
            Paragraph("VENTAS<br/>CONTRATOS<br/>(MWh)", st_hdr),
            Paragraph("VENTAS<br/>OPORTUNIDAD<br/>(MWh)", st_hdr),
            Paragraph("TOTAL<br/>COMPRAS<br/>(MWh)", st_hdr),
            Paragraph("TOTAL<br/>VENTAS<br/>(MWh)", st_hdr),
        ]
    ]

    for _, r in df_anual.iterrows():
        filas_tabla.append([
            r["Fecha"],
            _fmt_celda(r["Compras_Contratos"]),
            _fmt_celda(r["Compras_Oportunidad"]),
            _fmt_celda(r["Ventas_Contratos"]),
            _fmt_celda(r["Ventas_Oportunidad"]),
            _fmt_celda(r["Total_Compras"]),
            _fmt_celda(r["Total_Ventas"]),
        ])

    tot_cc = float(df_anual["Compras_Contratos"].dropna().sum())
    tot_co = float(df_anual["Compras_Oportunidad"].dropna().sum())
    tot_vc = float(df_anual["Ventas_Contratos"].dropna().sum())
    tot_vo = float(df_anual["Ventas_Oportunidad"].dropna().sum())
    tot_tc = float(df_anual["Total_Compras"].dropna().sum())
    tot_tv = float(df_anual["Total_Ventas"].dropna().sum())

    filas_tabla.append([
        "Total",
        _fmt_celda(tot_cc),
        _fmt_celda(tot_co),
        _fmt_celda(tot_vc),
        _fmt_celda(tot_vo),
        _fmt_celda(tot_tc),
        _fmt_celda(tot_tv),
    ])

    color_azul_hdr = colors.HexColor("#1B365D")
    color_zebra = colors.HexColor("#F4F7FA")
    color_tot_bg = colors.HexColor("#D9E2EC")

    ancho_cols = [1.8 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm, 2.75 * cm, 2.7 * cm, 2.7 * cm]
    tabla = Table(filas_tabla, colWidths=ancho_cols, repeatRows=1)

    estilos = [
        ("BACKGROUND", (0, 0), (-1, 0), color_azul_hdr),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),

        ("FONTNAME", (0, 1), (-1, -2), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -2), 8.2),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 1), (-1, -2), 3.2),
        ("BOTTOMPADDING", (0, 1), (-1, -2), 3.2),

        ("BACKGROUND", (0, -1), (-1, -1), color_tot_bg),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 8.5),
        ("TOPPADDING", (0, -1), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 4.5),

        ("BOX", (0, 0), (-1, -1), 1.1, colors.HexColor("#1B365D")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, colors.HexColor("#1B365D")),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, colors.HexColor("#1B365D")),
        ("INNERGRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#B8C4D0")),
    ]

    for idx_fila in range(2, len(filas_tabla) - 1, 2):
        estilos.append(("BACKGROUND", (0, idx_fila), (-1, idx_fila), color_zebra))

    tabla.setStyle(TableStyle(estilos))

    buf_retiros = _crear_grafica_barras_apiladas(df_anual, modo="compras")
    buf_inyecciones = _crear_grafica_barras_apiladas(df_anual, modo="ventas")

    img_retiros = Image(buf_retiros, width=17.8 * cm, height=6.1 * cm)
    img_inyecciones = Image(buf_inyecciones, width=17.8 * cm, height=6.1 * cm)

    doc = SimpleDocTemplate(
        ruta_salida,
        pagesize=letter,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
    )

    elementos = [
        encabezado_doc,
        Spacer(1, 0.35 * cm),
        tabla,
        Spacer(1, 0.4 * cm),
        KeepTogether([img_retiros, Spacer(1, 0.25 * cm), img_inyecciones]),
    ]
    doc.build(elementos)
    return ruta_salida
