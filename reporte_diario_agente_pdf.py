import os
from datetime import datetime
import pandas as pd

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    Image,
)

from db_mer import ejecutar_consulta_df, ejecutar_sql, es_postgres, ESQUEMA_PG
from catalogo_oficial_mer import DIRECTORIO_AGENTES_MER

DIAS_SEMANA_ES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]

MESES_LARGO_ES = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}


def _limpiar_str(val):
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s.lower() in ("nan", "none", "null", "<na>"):
        return ""
    return s


def formatear_fecha_larga_es(fecha_str):
    """Convierte '2026-11-24' -> 'martes, 24 de noviembre de 2026'."""
    dt = datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d")
    dia_sem = DIAS_SEMANA_ES[dt.weekday()]
    mes_nom = MESES_LARGO_ES[dt.month]
    return f"{dia_sem}, {dt.day} de {mes_nom} de {dt.year}"


def _asegurar_columnas_diario(conn):
    """Garantiza que existan las columnas de puntos de medida en TCP y TOP."""
    cur = conn.cursor()
    try:
        if es_postgres(conn):
            cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
            ejecutar_sql(cur, "ALTER TABLE fact_contratos_tcp ADD COLUMN IF NOT EXISTS punto_medida TEXT DEFAULT '';")
            ejecutar_sql(cur, "ALTER TABLE fact_contratos_tcp ADD COLUMN IF NOT EXISTS punto_medida_contraparte TEXT DEFAULT '';")
            ejecutar_sql(cur, "ALTER TABLE fact_oportunidad_top ADD COLUMN IF NOT EXISTS punto_medida TEXT DEFAULT '';")
        else:
            cur.execute("PRAGMA table_info(fact_contratos_tcp);")
            cols_tcp = [r[1].lower() for r in cur.fetchall()]
            if "punto_medida" not in cols_tcp:
                ejecutar_sql(cur, "ALTER TABLE fact_contratos_tcp ADD COLUMN punto_medida TEXT DEFAULT '';")
            if "punto_medida_contraparte" not in cols_tcp:
                ejecutar_sql(cur, "ALTER TABLE fact_contratos_tcp ADD COLUMN punto_medida_contraparte TEXT DEFAULT '';")

            cur.execute("PRAGMA table_info(fact_oportunidad_top);")
            cols_top = [r[1].lower() for r in cur.fetchall()]
            if "punto_medida" not in cols_top:
                ejecutar_sql(cur, "ALTER TABLE fact_oportunidad_top ADD COLUMN punto_medida TEXT DEFAULT '';")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()


def _obtener_mapa_agentes(conn):
    mapa = {
        _limpiar_str(cod): _limpiar_str(nom)
        for cod, (nom, _) in DIRECTORIO_AGENTES_MER.items()
    }
    try:
        df = ejecutar_consulta_df(
            "SELECT codigo_agente, nombre_agente FROM dim_catalogo_agentes",
            conn,
        )
        for _, r in df.iterrows():
            cod = _limpiar_str(r["codigo_agente"])
            nom = _limpiar_str(r["nombre_agente"])
            if cod and nom:
                mapa[cod] = nom
    except Exception:
        pass
    return mapa


def construir_datos_diarios_agente(conn, fecha_operacion, codigo_agente):
    """
    Construye la estructura horaria (00:00 a 23:00) y metadatos de columnas
    para el Informe Diario de Transacciones Regionales de un agente nacional.
    """
    _asegurar_columnas_diario(conn)

    ag_clean = _limpiar_str(codigo_agente)
    fecha_clean = str(fecha_operacion)[:10]
    mapa_ag = _obtener_mapa_agentes(conn)

    try:
        df_tcp = ejecutar_consulta_df(
            """
            SELECT strftime('%H', fecha_hora) AS hora,
                   TRIM(COALESCE(agente_contraparte, '')) AS contraparte,
                   TRIM(COALESCE(punto_medida_contraparte, '')) AS pm_cp,
                   TRIM(COALESCE(punto_medida, '')) AS pm_nac,
                   LOWER(TRIM(COALESCE(tipo_transaccion, ''))) AS tipo,
                   UPPER(TRIM(COALESCE(tipo_contrato, ''))) AS tipo_contrato,
                   LOWER(TRIM(COALESCE(tipo_oferta, ''))) AS tipo_oferta,
                   COALESCE(mw_predespachados, 0.0) AS mwh
            FROM fact_contratos_tcp
            WHERE fecha_operacion = ?
              AND TRIM(codigo_agente) = ?
              AND ABS(COALESCE(mw_predespachados, 0.0)) > 1e-9
            """,
            conn,
            params=[fecha_clean, ag_clean],
        )
    except Exception:
        df_tcp = ejecutar_consulta_df(
            """
            SELECT strftime('%H', fecha_hora) AS hora,
                   TRIM(COALESCE(agente_contraparte, '')) AS contraparte,
                   '' AS pm_cp,
                   '' AS pm_nac,
                   LOWER(TRIM(COALESCE(tipo_transaccion, ''))) AS tipo,
                   UPPER(TRIM(COALESCE(tipo_contrato, ''))) AS tipo_contrato,
                   LOWER(TRIM(COALESCE(tipo_oferta, ''))) AS tipo_oferta,
                   COALESCE(mw_predespachados, 0.0) AS mwh
            FROM fact_contratos_tcp
            WHERE fecha_operacion = ?
              AND TRIM(codigo_agente) = ?
              AND ABS(COALESCE(mw_predespachados, 0.0)) > 1e-9
            """,
            conn,
            params=[fecha_clean, ag_clean],
        )

    try:
        df_top = ejecutar_consulta_df(
            """
            SELECT strftime('%H', fecha_hora) AS hora,
                   TRIM(COALESCE(punto_medida, '')) AS pm_nac,
                   LOWER(TRIM(COALESCE(tipo_transaccion, ''))) AS tipo,
                   COALESCE(precio_exante, 0.0) AS precio,
                   COALESCE(mw_predespachados, 0.0) AS mwh
            FROM fact_oportunidad_top
            WHERE fecha_operacion = ?
              AND TRIM(codigo_agente) = ?
              AND ABS(COALESCE(mw_predespachados, 0.0)) > 1e-9
            """,
            conn,
            params=[fecha_clean, ag_clean],
        )
    except Exception:
        df_top = ejecutar_consulta_df(
            """
            SELECT strftime('%H', fecha_hora) AS hora,
                   '' AS pm_nac,
                   LOWER(TRIM(COALESCE(tipo_transaccion, ''))) AS tipo,
                   COALESCE(precio_exante, 0.0) AS precio,
                   COALESCE(mw_predespachados, 0.0) AS mwh
            FROM fact_oportunidad_top
            WHERE fecha_operacion = ?
              AND TRIM(codigo_agente) = ?
              AND ABS(COALESCE(mw_predespachados, 0.0)) > 1e-9
            """,
            conn,
            params=[fecha_clean, ag_clean],
        )

    horas = [f"{h:02d}" for h in range(24)]

    es_enee = "ENEE" in ag_clean.upper()
    df_flex = pd.DataFrame()
    if not df_tcp.empty and es_enee:
        mask_flex = (
            (df_tcp["tipo"] == "i")
            & (df_tcp["tipo_contrato"] == "CNFFF")
            & (df_tcp["tipo_oferta"] == "o.i. flexibilidad")
        )
        df_flex = df_tcp[mask_flex].copy()
        df_tcp = df_tcp[~mask_flex].copy()

    compras_cols = []
    ventas_cols = []

    if not df_tcp.empty:
        df_tcp["contraparte"] = df_tcp["contraparte"].apply(_limpiar_str)
        df_tcp["pm_cp"] = df_tcp["pm_cp"].apply(_limpiar_str)
        df_tcp["pm_nac"] = df_tcp["pm_nac"].apply(_limpiar_str)

        for tipo_tr, destino in [("r", compras_cols), ("i", ventas_cols)]:
            sub = df_tcp[df_tcp["tipo"] == tipo_tr]
            if sub.empty:
                continue
            pares = (
                sub[["contraparte", "pm_cp", "pm_nac"]]
                .drop_duplicates()
                .sort_values(["contraparte", "pm_cp", "pm_nac"])
            )
            for _, par in pares.iterrows():
                cp = par["contraparte"]
                pm_cp = par["pm_cp"]
                pm_nac = par["pm_nac"]
                grp = (
                    sub[
                        (sub["contraparte"] == cp)
                        & (sub["pm_cp"] == pm_cp)
                        & (sub["pm_nac"] == pm_nac)
                    ]
                    .groupby("hora")["mwh"]
                    .sum()
                    .to_dict()
                )
                destino.append({
                    "cp": cp,
                    "nombre_cp": mapa_ag.get(cp) or cp,
                    "pm_cp": pm_cp or "-",
                    "pm_nac": pm_nac or "-",
                    "valores": {h: float(grp.get(h, 0.0)) for h in horas},
                })

    op_compra_pm = ""
    op_venta_pm = ""
    op_compra_mwh = {h: 0.0 for h in horas}
    op_compra_usd = {h: 0.0 for h in horas}
    op_venta_mwh = {h: 0.0 for h in horas}
    op_venta_usd = {h: 0.0 for h in horas}

    if not df_top.empty:
        df_top["pm_nac"] = df_top["pm_nac"].apply(_limpiar_str)
        for tipo_tr, dict_mwh, dict_usd, is_compra in [
            ("r", op_compra_mwh, op_compra_usd, True),
            ("i", op_venta_mwh, op_venta_usd, False),
        ]:
            sub_op = df_top[df_top["tipo"] == tipo_tr]
            if not sub_op.empty:
                pms = [p for p in sub_op["pm_nac"].unique().tolist() if p]
                pm_txt = " / ".join(sorted(pms))
                if is_compra:
                    op_compra_pm = pm_txt
                else:
                    op_venta_pm = pm_txt

                for h, grp in sub_op.groupby("hora"):
                    tot_m = float(grp["mwh"].sum())
                    dict_mwh[h] = tot_m
                    if tot_m > 1e-9:
                        dict_usd[h] = float((grp["mwh"] * grp["precio"]).sum() / tot_m)
                    else:
                        dict_usd[h] = float(grp["precio"].mean())

    if not df_flex.empty:
        for h, grp in df_flex.groupby("hora"):
            op_venta_mwh[h] += float(grp["mwh"].abs().sum())

    filas_df = []
    for h in horas:
        fila = {"Hora": f"{h}:00"}
        tot_comp = 0.0
        for idx, c_info in enumerate(compras_cols, 1):
            val = c_info["valores"][h]
            tot_comp += val
            etiq = f"{c_info['cp']} ({c_info['pm_nac']})"
            if etiq in fila:
                etiq = f"{etiq} #{idx}"
            fila[etiq] = val
        if compras_cols:
            fila["Total Compra (MWh)"] = tot_comp

        tot_vent = 0.0
        for idx, v_info in enumerate(ventas_cols, 1):
            val = v_info["valores"][h]
            tot_vent += val
            etiq = f"Venta {v_info['cp']} ({v_info['pm_nac']})"
            if etiq in fila:
                etiq = f"{etiq} #{idx}"
            fila[etiq] = val
        if ventas_cols:
            fila["Total Venta (MWh)"] = tot_vent

        fila["Compras Op. (MWh)"] = op_compra_mwh[h]
        fila["Compras Op. (US$/MWh)"] = op_compra_usd[h]
        fila["Ventas Op. (MWh)"] = op_venta_mwh[h]
        fila["Ventas Op. (US$/MWh)"] = op_venta_usd[h]
        filas_df.append(fila)

    df_resumen = pd.DataFrame(filas_df)

    return {
        "fecha": fecha_clean,
        "fecha_larga": formatear_fecha_larga_es(fecha_clean),
        "codigo_agente": ag_clean,
        "nombre_agente": mapa_ag.get(ag_clean) or ag_clean,
        "horas": horas,
        "compras_cols": compras_cols,
        "ventas_cols": ventas_cols,
        "op_compra_pm": op_compra_pm,
        "op_venta_pm": op_venta_pm,
        "op_compra_mwh": op_compra_mwh,
        "op_compra_usd": op_compra_usd,
        "op_venta_mwh": op_venta_mwh,
        "op_venta_usd": op_venta_usd,
        "df_resumen": df_resumen,
    }


def _crear_logo_cnd_ajustado(ruta_logo, alto_objetivo=16 * mm, max_ancho=68 * mm):
    if not ruta_logo or not os.path.exists(ruta_logo):
        return ""
    try:
        ir = ImageReader(ruta_logo)
        w_px, h_px = ir.getSize()
        if w_px <= 0 or h_px <= 0:
            return ""
        escala = alto_objetivo / float(h_px)
        w_pt = float(w_px) * escala
        h_pt = alto_objetivo
        if w_pt > max_ancho:
            escala_w = max_ancho / w_pt
            w_pt *= escala_w
            h_pt *= escala_w
        img = Image(ruta_logo, width=w_pt, height=h_pt)
        img.hAlign = "LEFT"
        return img
    except Exception:
        return ""


def generar_pdf_diario_agente_cnd(
    conn,
    fecha_operacion,
    codigo_agente,
    ruta_logo="logo_cnd.png",
    elaborado_por="",
    ruta_salida=None,
):
    """
    Genera el PDF institucional 'INFORME DE TRANSACCIONES REGIONALES' diario por agente.
    """
    datos = construir_datos_diarios_agente(conn, fecha_operacion, codigo_agente)
    ag_clean = datos["codigo_agente"]
    fecha_clean = datos["fecha"]

    if not ruta_salida:
        ruta_salida = f"Informe_Transacciones_Regionales_{ag_clean}_{fecha_clean.replace('-', '')}.pdf"

    compras_cols = datos["compras_cols"]
    ventas_cols = datos["ventas_cols"]
    n_comp = len(compras_cols)
    n_vent = len(ventas_cols)

    total_cols = 1 + (n_comp + 1 if n_comp > 0 else 0) + (n_vent + 1 if n_vent > 0 else 0) + 4
    usar_landscape = total_cols > 7
    pagesize = landscape(letter) if usar_landscape else letter

    doc = SimpleDocTemplate(
        ruta_salida,
        pagesize=pagesize,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    ancho_util = doc.width

    st_titulo = ParagraphStyle(
        "TituloCND",
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        alignment=TA_CENTER,
    )
    st_subtitulo = ParagraphStyle(
        "SubtituloCND",
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        alignment=TA_CENTER,
    )
    st_fecha = ParagraphStyle(
        "FechaCND",
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        alignment=TA_CENTER,
    )
    st_hdr = ParagraphStyle(
        "HdrTabla",
        fontName="Helvetica-Bold",
        fontSize=7.2,
        leading=8.8,
        alignment=TA_CENTER,
    )
    st_hdr_small = ParagraphStyle(
        "HdrTablaSmall",
        fontName="Helvetica-Bold",
        fontSize=6.8,
        leading=8.2,
        alignment=TA_CENTER,
    )
    st_firma = ParagraphStyle(
        "FirmaCND",
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        alignment=TA_CENTER,
    )

    elementos = []

    logo_obj = _crear_logo_cnd_ajustado(ruta_logo, alto_objetivo=15 * mm, max_ancho=68 * mm)
    col_logo_w = 72 * mm
    col_tit_w = ancho_util - col_logo_w

    bloque_titulos = [
        Paragraph("INFORME DE TRANSACCIONES REGIONALES", st_titulo),
        Paragraph(f"AGENTE {ag_clean}", st_subtitulo),
        Paragraph(datos["fecha_larga"], st_fecha),
    ]
    tbl_encabezado = Table(
        [[logo_obj, bloque_titulos]],
        colWidths=[col_logo_w, col_tit_w],
    )
    tbl_encabezado.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (1, 0), (1, 0), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ])
    )
    elementos.append(tbl_encabezado)
    elementos.append(Spacer(1, 3 * mm))

    mostrar_bloque_compra = n_comp > 0 or (n_comp == 0 and n_vent == 0)
    cols_bloque_venta = n_vent

    fila0 = [Paragraph("HORA", st_hdr)]
    fila1 = [""]
    fila2 = [""]
    fila3 = [""]
    fila4 = [""]
    fila5 = [""]

    spans = [
        ("SPAN", (0, 0), (0, 5)),
    ]

    col_idx = 1

    if mostrar_bloque_compra:
        if n_comp > 0:
            for c_info in compras_cols:
                fila0.append(Paragraph(c_info["nombre_cp"], st_hdr))
                fila1.append(Paragraph(c_info["cp"], st_hdr))
                fila2.append(Paragraph("Compra de Energía (MWh)", st_hdr_small))
                fila3.append(Paragraph("Punto Medida Contraparte /<br/>Punto de Medida", st_hdr_small))
                fila4.append(Paragraph(f"{c_info['pm_cp']}<br/>{c_info['pm_nac']}", st_hdr_small))
                fila5.append("")
                spans.append(("SPAN", (col_idx, 4), (col_idx, 5)))
                col_idx += 1
        else:
            fila0.append(Paragraph("SIN CONTRATOS", st_hdr))
            fila1.append(Paragraph("-", st_hdr))
            fila2.append(Paragraph("Compra de Energía (MWh)", st_hdr_small))
            fila3.append(Paragraph("Punto de Medida", st_hdr_small))
            fila4.append(Paragraph("-", st_hdr_small))
            fila5.append("")
            spans.append(("SPAN", (col_idx, 4), (col_idx, 5)))
            col_idx += 1

        fila0.append(Paragraph("Total Compra de Energía (MWh)", st_hdr))
        fila1.append("")
        fila2.append("")
        fila3.append("")
        fila4.append("")
        fila5.append("")
        spans.append(("SPAN", (col_idx, 0), (col_idx, 5)))
        col_idx += 1

    if cols_bloque_venta > 0:
        for v_info in ventas_cols:
            fila0.append(Paragraph(v_info["nombre_cp"], st_hdr))
            fila1.append(Paragraph(v_info["cp"], st_hdr))
            fila2.append(Paragraph("Venta de Energía (MWh)", st_hdr_small))
            fila3.append(Paragraph("Punto Medida Contraparte /<br/>Punto de Medida", st_hdr_small))
            fila4.append(Paragraph(f"{v_info['pm_cp']}<br/>{v_info['pm_nac']}", st_hdr_small))
            fila5.append("")
            spans.append(("SPAN", (col_idx, 4), (col_idx, 5)))
            col_idx += 1

        fila0.append(Paragraph("Total Venta de Energía (MWh)", st_hdr))
        fila1.append("")
        fila2.append("")
        fila3.append("")
        fila4.append("")
        fila5.append("")
        spans.append(("SPAN", (col_idx, 0), (col_idx, 5)))
        col_idx += 1

    c_op_comp_ini = col_idx
    fila0.extend([Paragraph("COMPRAS OPORTUNIDAD", st_hdr), ""])
    fila1.extend(["", ""])
    fila2.extend(["", ""])
    fila3.extend([Paragraph("Punto de Medida", st_hdr_small), ""])
    fila4.extend([Paragraph(datos["op_compra_pm"] or "-", st_hdr_small), ""])
    fila5.extend([Paragraph("MWh", st_hdr_small), Paragraph("US$/MWh", st_hdr_small)])
    spans.append(("SPAN", (c_op_comp_ini, 0), (c_op_comp_ini + 1, 2)))
    spans.append(("SPAN", (c_op_comp_ini, 3), (c_op_comp_ini + 1, 3)))
    spans.append(("SPAN", (c_op_comp_ini, 4), (c_op_comp_ini + 1, 4)))
    col_idx += 2

    c_op_vent_ini = col_idx
    fila0.extend([Paragraph("VENTAS OPORTUNIDAD", st_hdr), ""])
    fila1.extend(["", ""])
    fila2.extend(["", ""])
    fila3.extend([Paragraph("Punto de Medida", st_hdr_small), ""])
    fila4.extend([Paragraph(datos["op_venta_pm"] or "-", st_hdr_small), ""])
    fila5.extend([Paragraph("MWh", st_hdr_small), Paragraph("US$/MWh", st_hdr_small)])
    spans.append(("SPAN", (c_op_vent_ini, 0), (c_op_vent_ini + 1, 2)))
    spans.append(("SPAN", (c_op_vent_ini, 3), (c_op_vent_ini + 1, 3)))
    spans.append(("SPAN", (c_op_vent_ini, 4), (c_op_vent_ini + 1, 4)))
    col_idx += 2

    data_tabla = [fila0, fila1, fila2, fila3, fila4, fila5]

    totales_comp_cols = [0.0] * max(n_comp, 1)
    totales_vent_cols = [0.0] * n_vent
    tot_op_comp_mwh = 0.0
    tot_op_comp_monto = 0.0
    tot_op_vent_mwh = 0.0
    tot_op_vent_monto = 0.0

    for h in datos["horas"]:
        fila_h = [f"{h}:00"]
        if mostrar_bloque_compra:
            if n_comp > 0:
                suma_h_comp = 0.0
                for idx, c_info in enumerate(compras_cols):
                    v = c_info["valores"][h]
                    totales_comp_cols[idx] += v
                    suma_h_comp += v
                    fila_h.append(f"{v:,.3f}")
                fila_h.append(f"{suma_h_comp:,.3f}")
            else:
                fila_h.extend(["0.000", "0.000"])

        if cols_bloque_venta > 0:
            suma_h_vent = 0.0
            for idx, v_info in enumerate(ventas_cols):
                v = v_info["valores"][h]
                totales_vent_cols[idx] += v
                suma_h_vent += v
                fila_h.append(f"{v:,.3f}")
            fila_h.append(f"{suma_h_vent:,.3f}")

        oc_m = datos["op_compra_mwh"][h]
        oc_u = datos["op_compra_usd"][h]
        ov_m = datos["op_venta_mwh"][h]
        ov_u = datos["op_venta_usd"][h]

        tot_op_comp_mwh += oc_m
        tot_op_comp_monto += oc_m * oc_u
        tot_op_vent_mwh += ov_m
        tot_op_vent_monto += ov_m * ov_u

        fila_h.extend([
            f"{oc_m:,.2f}",
            f"{oc_u:,.2f}",
            f"{ov_m:,.2f}",
            f"{ov_u:,.2f}",
        ])
        data_tabla.append(fila_h)

    fila_tot = ["TOTAL"]
    if mostrar_bloque_compra:
        if n_comp > 0:
            for t_c in totales_comp_cols:
                fila_tot.append(f"{t_c:,.3f}")
            fila_tot.append(f"{sum(totales_comp_cols):,.3f}")
        else:
            fila_tot.extend(["0.000", "0.000"])

    if cols_bloque_venta > 0:
        for t_v in totales_vent_cols:
            fila_tot.append(f"{t_v:,.3f}")
        fila_tot.append(f"{sum(totales_vent_cols):,.3f}")

    prom_pond_comp = (tot_op_comp_monto / tot_op_comp_mwh) if tot_op_comp_mwh > 1e-9 else 0.0
    prom_pond_vent = (tot_op_vent_monto / tot_op_vent_mwh) if tot_op_vent_mwh > 1e-9 else 0.0
    fila_tot.extend([
        f"{tot_op_comp_mwh:,.2f}",
        f"{prom_pond_comp:,.2f}",
        f"{tot_op_vent_mwh:,.2f}",
        f"{prom_pond_vent:,.2f}",
    ])
    data_tabla.append(fila_tot)

    num_columnas = len(fila0)
    w_hora = 15 * mm
    w_resto = (ancho_util - w_hora) / max(num_columnas - 1, 1)
    col_widths = [w_hora] + [w_resto] * (num_columnas - 1)

    tabla = Table(data_tabla, colWidths=col_widths, repeatRows=6)

    estilo = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BOX", (0, 0), (-1, -1), 1.0, colors.black),
        ("BOX", (0, 0), (-1, 5), 1.0, colors.black),
        ("FONTNAME", (0, 0), (-1, 5), "Helvetica-Bold"),
        ("FONTNAME", (0, 6), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 6), (-1, -1), 7.3),
        ("ALIGN", (0, 0), (-1, 5), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 6), (0, -1), "CENTER"),
        ("ALIGN", (1, 6), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, 5), colors.HexColor("#F8FAFC")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EEF2F6")),
        ("TOPPADDING", (0, 0), (-1, 5), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, 5), 2.2),
        ("TOPPADDING", (0, 6), (-1, -1), 1.6),
        ("BOTTOMPADDING", (0, 6), (-1, -1), 1.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
    ]
    estilo.extend(spans)
    tabla.setStyle(TableStyle(estilo))

    elementos.append(tabla)
    elementos.append(Spacer(1, 10 * mm))

    nombre_firma = _limpiar_str(elaborado_por) or "____________________________________"
    lineas_firma = [
        [Paragraph("________________________________________", st_firma)],
        [Paragraph(f"<b>Elaborado por:</b> {nombre_firma}", st_firma)],
        [Paragraph("Departamento Liquidación de Transacciones de Mercado", st_firma)],
    ]
    tbl_firma = Table(lineas_firma, colWidths=[110 * mm])
    tbl_firma.hAlign = "CENTER"
    tbl_firma.setStyle(
        TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ])
    )
    elementos.append(tbl_firma)

    doc.build(elementos)
    return ruta_salida
