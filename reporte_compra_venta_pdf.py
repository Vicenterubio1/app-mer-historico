import os
from datetime import datetime

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
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from consultar_mer import construir_df_balance_horario, formatear_fecha_corta_es


def _formatear_fecha_larga_es(fecha_str):
    """Convierte '2026-11-19' en 'jueves, 19 de noviembre de 2026'."""
    dt = datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d")
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return f"{dias[dt.weekday()]}, {dt.day} de {meses[dt.month - 1]} de {dt.year}"


def _construir_logo_proporcional(ruta_logo, max_w, max_h, estilo_fallback):
    """Carga el logo manteniendo su relación de aspecto original sin deformarlo."""
    if ruta_logo and os.path.exists(ruta_logo):
        try:
            lector = ImageReader(ruta_logo)
            iw, ih = lector.getSize()
            if iw > 0 and ih > 0:
                escala = min(max_w / float(iw), max_h / float(ih))
                return RLImage(ruta_logo, width=iw * escala, height=ih * escala)
        except Exception:
            pass
    return Paragraph("<b>CENTRO NACIONAL<br/>DE DESPACHO</b>", estilo_fallback)


def generar_pdf_informe_compra_venta(
    conn,
    fecha_inicio,
    fecha_fin=None,
    pais_filtro=None,
    ruta_logo="logo_cnd.png",
):
    """Genera el PDF del Informe de Compra y Venta de Energía Regional usando ReportLab."""
    f_ini = str(fecha_inicio)[:10]
    f_fin = str(fecha_fin)[:10] if fecha_fin else f_ini

    df_bal = construir_df_balance_horario(conn, f_ini, f_fin, pais_filtro)

    if f_ini == f_fin:
        subtitulo_fecha = _formatear_fecha_larga_es(f_ini)
        nombre_archivo = f"Informe_Compra_Venta_MER_{f_ini}_{pais_filtro or 'REGIONAL'}.pdf"
    else:
        subtitulo_fecha = f"Del {_formatear_fecha_larga_es(f_ini)} al {_formatear_fecha_larga_es(f_fin)}"
        nombre_archivo = f"Informe_Compra_Venta_MER_{f_ini}_a_{f_fin}_{pais_filtro or 'REGIONAL'}.pdf"

    doc = SimpleDocTemplate(
        nombre_archivo,
        pagesize=letter,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )

    elementos = []
    styles = getSampleStyleSheet()

    estilo_cnd = ParagraphStyle(
        "EstiloCND",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=11.5,
        alignment=TA_LEFT,
    )
    estilo_titulo = ParagraphStyle(
        "EstiloTitulo",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        alignment=TA_CENTER,
    )
    estilo_subtitulo = ParagraphStyle(
        "EstiloSubtitulo",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        alignment=TA_CENTER,
    )

    logo_cell = _construir_logo_proporcional(
        ruta_logo=ruta_logo,
        max_w=2.05 * inch,
        max_h=0.68 * inch,
        estilo_fallback=estilo_cnd,
    )

    titulo_texto = "INFORME DE COMPRA Y VENTA DE ENERGÍA REGIONAL"
    if pais_filtro:
        titulo_texto += f" ({pais_filtro})"

    enc_der = Table(
        [
            [Paragraph(titulo_texto, estilo_titulo)],
            [Paragraph(subtitulo_fecha, estilo_subtitulo)],
        ],
        colWidths=[5.4 * inch],
    )
    enc_der.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ])
    )

    tabla_encabezado = Table([[logo_cell, enc_der]], colWidths=[2.2 * inch, 5.4 * inch])
    tabla_encabezado.setStyle(
        TableStyle([
            ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
            ("LINEBEFORE", (1, 0), (1, 0), 1.0, colors.black),
            ("ALIGN", (0, 0), (0, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    elementos.append(tabla_encabezado)
    elementos.append(Spacer(1, 0.08 * inch))

    fila_hdr_1 = ["FECHA", "HORA", "RETIROS", "", "TOTAL\nCOMPRAS", "INYECCIONES", "", "TOTAL\nVENTAS"]
    fila_hdr_2 = ["", "", "CONTRATOS", "OPORTUNIDAD", "", "CONTRATOS", "OPORTUNIDAD", ""]
    fila_hdr_3 = ["", "", "MWh", "MWh", "MWh", "MWh", "MWh", "MWh"]

    filas_datos = []
    tot_ret_c = 0.0
    tot_ret_o = 0.0
    tot_compras = 0.0
    tot_iny_c = 0.0
    tot_iny_o = 0.0
    tot_ventas = 0.0

    for _, r in df_bal.iterrows():
        ret_c = float(r.get("ret_contratos") or 0.0)
        ret_o = float(r.get("ret_oportunidad") or 0.0)
        iny_c = float(r.get("iny_contratos") or 0.0)
        iny_o = float(r.get("iny_oportunidad") or 0.0)
        compras_h = ret_c + ret_o
        ventas_h = iny_c + iny_o

        tot_ret_c += ret_c
        tot_ret_o += ret_o
        tot_compras += compras_h
        tot_iny_c += iny_c
        tot_iny_o += iny_o
        tot_ventas += ventas_h

        hora_txt = str(r.get("hora") or "").strip()
        if len(hora_txt) <= 2 and hora_txt.isdigit():
            hora_txt = f"{int(hora_txt):02d}:00"

        filas_datos.append([
            formatear_fecha_corta_es(r.get("fecha")),
            hora_txt,
            f"{ret_c:.2f}",
            f"{ret_o:.2f}",
            f"{compras_h:.2f}",
            f"{iny_c:.2f}",
            f"{iny_o:.2f}",
            f"{ventas_h:.2f}",
        ])

    fila_total = [
        "TOTAL",
        "",
        f"{tot_ret_c:,.2f}",
        "-" if abs(tot_ret_o) < 1e-6 else f"{tot_ret_o:,.2f}",
        f"{tot_compras:,.2f}",
        f"{tot_iny_c:,.2f}",
        "-" if abs(tot_iny_o) < 1e-6 else f"{tot_iny_o:,.2f}",
        f"{tot_ventas:,.2f}",
    ]

    datos_tabla = [fila_hdr_1, fila_hdr_2, fila_hdr_3] + filas_datos + [fila_total]
    anchos_col = [0.95 * inch, 0.70 * inch, 1.00 * inch, 1.05 * inch, 0.95 * inch, 0.95 * inch, 0.95 * inch, 1.05 * inch]

    tabla_principal = Table(datos_tabla, colWidths=anchos_col, repeatRows=3)
    idx_total = len(datos_tabla) - 1

    estilo_tabla = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
        ("SPAN", (0, 0), (0, 2)),
        ("SPAN", (1, 0), (1, 2)),
        ("SPAN", (2, 0), (3, 0)),
        ("SPAN", (4, 0), (4, 1)),
        ("SPAN", (5, 0), (6, 0)),
        ("SPAN", (7, 0), (7, 1)),
        ("SPAN", (0, idx_total), (1, idx_total)),
        ("FONTNAME", (0, 0), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 2), 8.0),
        ("BACKGROUND", (0, 0), (-1, 2), colors.HexColor("#F2F4F7")),
        ("ALIGN", (0, 0), (-1, 2), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 3), (-1, idx_total - 1), "Helvetica"),
        ("FONTSIZE", (0, 3), (-1, idx_total - 1), 7.8),
        ("ALIGN", (0, 3), (1, idx_total - 1), "CENTER"),
        ("ALIGN", (2, 3), (-1, idx_total - 1), "RIGHT"),
        ("FONTNAME", (0, idx_total), (-1, idx_total), "Helvetica-Bold"),
        ("FONTSIZE", (0, idx_total), (-1, idx_total), 8.2),
        ("ALIGN", (0, idx_total), (1, idx_total), "LEFT"),
        ("ALIGN", (2, idx_total), (-1, idx_total), "RIGHT"),
        ("LINEABOVE", (0, idx_total), (-1, idx_total), 1.1, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 1.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ])
    tabla_principal.setStyle(estilo_tabla)
    elementos.append(tabla_principal)

    doc.build(elementos)
    return nombre_archivo
