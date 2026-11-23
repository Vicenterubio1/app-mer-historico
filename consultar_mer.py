import os
import calendar
from datetime import datetime
import pandas as pd

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from db_mer import obtener_conexion, ejecutar_consulta_df

PREFIJO_PAIS = {
    "1": "GT",
    "2": "SV",
    "3": "HN",
    "4": "NI",
    "5": "CR",
    "6": "PA",
}

MESES_CORTOS_ES = {
    1: "ene", 2: "feb", 3: "mar", 4: "abr", 5: "may", 6: "jun",
    7: "jul", 8: "ago", 9: "sep", 10: "oct", 11: "nov", 12: "dic"
}

# Regla MER: En TCP, cuando Tipo Contrato = 'CNFFF' y Tipo Oferta = 'O.I. Flexibilidad'
# con MW Predespachados != 0, esa energía flexibilizada se asigna a Ventas de Oportunidad
# (sin restar las compras 'r' de Contratos, pero sin duplicarla en Ventas Contratos 'i').
CONDICION_FLEX_CNFFF_SQL = """
    UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
    AND LOWER(TRIM(COALESCE(tipo_oferta, ''))) = 'o.i. flexibilidad'
    AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
"""


def limpiar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def formatear_fecha_corta_es(fecha_iso):
    try:
        dt = datetime.strptime(str(fecha_iso)[:10], "%Y-%m-%d")
        return f"{dt.day}-{MESES_CORTOS_ES[dt.month]}-{str(dt.year)[2:]}"
    except Exception:
        return str(fecha_iso)


def obtener_diccionario_agentes(conn):
    try:
        df = ejecutar_consulta_df("SELECT codigo_agente, nombre_agente FROM dim_catalogo_agentes", conn)
        return {
            limpiar_codigo(r["codigo_agente"]): str(r["nombre_agente"] or "").strip()
            for _, r in df.iterrows()
        }
    except Exception:
        return {}


def construir_df_balance_horario(conn, f_ini, f_fin, pais_cod=None):
    params = [f_ini, f_fin]
    cond_pais = ""
    if pais_cod:
        cond_pais = " AND pais = ?"
        params.append(pais_cod)

    # Compras ('r') se mantienen íntegras; en Ventas ('i') de TCP se excluyen las flexibilizadas a Oportunidad
    df_tcp = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) AS hora,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND NOT (LOWER(TRIM(tipo_transaccion)) = 'i' AND ({CONDICION_FLEX_CNFFF_SQL}))
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora), LOWER(TRIM(tipo_transaccion))
    """, conn, params=params)

    df_top = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) AS hora,
               LOWER(TRIM(tipo_transaccion)) AS tipo,
               SUM(mw_predespachados) AS mwh
        FROM fact_oportunidad_top
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora), LOWER(TRIM(tipo_transaccion))
    """, conn, params=params)

    # Ventas de Oportunidad provenientes de CNFFF + O.I. Flexibilidad en TCP
    df_flex_ventas = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) AS hora,
               SUM(ABS(mw_predespachados)) AS mwh
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND {CONDICION_FLEX_CNFFF_SQL}
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora)
    """, conn, params=params)

    fechas = pd.date_range(f_ini, f_fin, freq="D").strftime("%Y-%m-%d").tolist()
    horas = [f"{h:02d}" for h in range(24)]
    grid = pd.MultiIndex.from_product([fechas, horas], names=["fecha", "hora"]).to_frame().reset_index(drop=True)

    def extraer_serie(df, tipo_val, col_dest):
        if df.empty:
            grid[col_dest] = 0.0
            return
        sub = df[df["tipo"] == tipo_val][["fecha", "hora", "mwh"]].rename(columns={"mwh": col_dest})
        sub["fecha"] = sub["fecha"].astype(str).str[:10]
        sub["hora"] = sub["hora"].astype(str).str.zfill(2)
        merged = grid.merge(sub, on=["fecha", "hora"], how="left")
        grid[col_dest] = merged[col_dest].fillna(0.0)

    extraer_serie(df_tcp, "r", "ret_contratos")
    extraer_serie(df_top, "r", "ret_oportunidad")
    extraer_serie(df_tcp, "i", "iny_contratos")
    extraer_serie(df_top, "i", "iny_oportunidad")

    if not df_flex_ventas.empty:
        sub_fl = df_flex_ventas[["fecha", "hora", "mwh"]].rename(columns={"mwh": "flex_ventas_mwh"})
        sub_fl["fecha"] = sub_fl["fecha"].astype(str).str[:10]
        sub_fl["hora"] = sub_fl["hora"].astype(str).str.zfill(2)
        merged_fl = grid.merge(sub_fl, on=["fecha", "hora"], how="left")
        grid["iny_oportunidad"] = grid["iny_oportunidad"] + merged_fl["flex_ventas_mwh"].fillna(0.0)

    return grid


def construir_df_tcp_agente_contraparte(conn, f_ini, f_fin, pais_cod=None):
    params = [f_ini, f_fin]
    cond_pais = ""
    if pais_cod:
        cond_pais = " AND pais = ?"
        params.append(pais_cod)

    df = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) || ':00' AS hora,
               pais AS pais,
               TRIM(codigo_agente) AS agente_nacional,
               TRIM(agente_contraparte) AS agente_contraparte,
               UPPER(TRIM(tipo_contrato)) AS tipo_contrato,
               COALESCE(TRIM(tipo_oferta), '') AS tipo_oferta,
               CASE LOWER(TRIM(tipo_transaccion))
                   WHEN 'r' THEN 'Compra (Retiro)'
                   WHEN 'i' THEN 'Venta (Inyección)'
                   ELSE tipo_transaccion
               END AS operacion,
               SUM(mw_requerida) AS mwh_requeridos,
               SUM(mw_declarada) AS mwh_declarados,
               AVG(precio_exante) AS precio_exante_usd,
               SUM(mw_predespachados) AS mwh_predespachados
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora), pais,
                 TRIM(codigo_agente), TRIM(agente_contraparte),
                 UPPER(TRIM(tipo_contrato)), COALESCE(TRIM(tipo_oferta), ''),
                 CASE LOWER(TRIM(tipo_transaccion))
                     WHEN 'r' THEN 'Compra (Retiro)'
                     WHEN 'i' THEN 'Venta (Inyección)'
                     ELSE tipo_transaccion
                 END
        ORDER BY fecha, hora, agente_nacional, agente_contraparte
    """, conn, params=params)

    if df.empty:
        return df

    df = df.rename(columns={
        "fecha": "Fecha",
        "hora": "Hora",
        "pais": "Pais",
        "agente_nacional": "Agente_Nacional",
        "agente_contraparte": "Agente_Contraparte",
        "tipo_contrato": "Tipo_Contrato",
        "tipo_oferta": "Tipo_Oferta",
        "operacion": "Operacion",
        "mwh_requeridos": "MWh_Requeridos",
        "mwh_declarados": "MWh_Declarados",
        "precio_exante_usd": "Precio_ExAnte_USD",
        "mwh_predespachados": "MWh_Predespachados",
    })
    df["Fecha"] = df["Fecha"].astype(str).str[:10]

    mapa_ag = obtener_diccionario_agentes(conn)
    df["Nombre_Agente"] = df["Agente_Nacional"].map(mapa_ag).fillna("")
    df["Nombre_Contraparte"] = df["Agente_Contraparte"].map(mapa_ag).fillna("")

    cols = [
        "Fecha", "Hora", "Pais", "Agente_Nacional", "Nombre_Agente",
        "Agente_Contraparte", "Nombre_Contraparte", "Tipo_Contrato",
        "Tipo_Oferta", "Operacion", "MWh_Requeridos", "MWh_Declarados",
        "Precio_ExAnte_USD", "MWh_Predespachados"
    ]
    return df[cols]


def construir_df_top_agente_hora(conn, f_ini, f_fin, pais_cod=None):
    params = [f_ini, f_fin]
    cond_pais = ""
    if pais_cod:
        cond_pais = " AND pais = ?"
        params.append(pais_cod)

    df_top = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) || ':00' AS hora,
               pais AS pais,
               TRIM(codigo_agente) AS agente_nacional,
               CASE LOWER(TRIM(tipo_transaccion))
                   WHEN 'r' THEN 'Compra (Retiro)'
                   WHEN 'i' THEN 'Venta (Inyección)'
                   ELSE tipo_transaccion
               END AS operacion,
               SUM(mw_requerida) AS mwh_requeridos,
               SUM(mw_declarada) AS mwh_declarados,
               AVG(precio_exante) AS precio_exante_usd,
               SUM(mw_predespachados) AS mwh_predespachados
        FROM fact_oportunidad_top
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora), pais,
                 TRIM(codigo_agente),
                 CASE LOWER(TRIM(tipo_transaccion))
                     WHEN 'r' THEN 'Compra (Retiro)'
                     WHEN 'i' THEN 'Venta (Inyección)'
                     ELSE tipo_transaccion
                 END
    """, conn, params=params)

    df_flex = ejecutar_consulta_df(f"""
        SELECT date(fecha_hora) AS fecha,
               strftime('%H', fecha_hora) || ':00' AS hora,
               pais AS pais,
               TRIM(codigo_agente) AS agente_nacional,
               'Venta (Inyección) [Flex. CNFFF]' AS operacion,
               SUM(mw_requerida) AS mwh_requeridos,
               SUM(mw_declarada) AS mwh_declarados,
               AVG(precio_exante) AS precio_exante_usd,
               SUM(ABS(mw_predespachados)) AS mwh_predespachados
        FROM fact_contratos_tcp
        WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
          AND {CONDICION_FLEX_CNFFF_SQL}
        GROUP BY date(fecha_hora), strftime('%H', fecha_hora), pais, TRIM(codigo_agente)
    """, conn, params=params)

    df = pd.concat([df_top, df_flex], ignore_index=True)
    if df.empty:
        return df

    df = df.sort_values(by=["fecha", "hora", "agente_nacional", "operacion"]).reset_index(drop=True)

    df = df.rename(columns={
        "fecha": "Fecha",
        "hora": "Hora",
        "pais": "Pais",
        "agente_nacional": "Agente_Nacional",
        "operacion": "Operacion",
        "mwh_requeridos": "MWh_Requeridos",
        "mwh_declarados": "MWh_Declarados",
        "precio_exante_usd": "Precio_ExAnte_USD",
        "mwh_predespachados": "MWh_Predespachados",
    })
    df["Fecha"] = df["Fecha"].astype(str).str[:10]

    mapa_ag = obtener_diccionario_agentes(conn)
    df["Nombre_Agente"] = df["Agente_Nacional"].map(mapa_ag).fillna("")

    cols = [
        "Fecha", "Hora", "Pais", "Agente_Nacional", "Nombre_Agente",
        "Operacion", "MWh_Requeridos", "MWh_Declarados",
        "Precio_ExAnte_USD", "MWh_Predespachados"
    ]
    return df[cols]


def construir_df_mensual_cnd(conn, anio: int, mes: int, agente_nacional: str, agente_contraparte: str, tipo_transaccion: str = "r"):
    """Construye la tabla diaria del mes (CF, CNFFF, Total) para un par Agente / Contraparte."""
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    f_ini = f"{anio:04d}-{mes:02d}-01"
    f_fin = f"{anio:04d}-{mes:02d}-{ultimo_dia:02d}"

    ag_nac_clean = agente_nacional.strip()
    pais_ag = PREFIJO_PAIS.get(ag_nac_clean[0], "") if ag_nac_clean else ""

    sql = f"""
        SELECT fecha_operacion AS fecha,
               UPPER(TRIM(tipo_contrato)) AS tipo_contrato,
               SUM(mw_predespachados) AS mwh
        FROM fact_contratos_tcp
        WHERE fecha_operacion BETWEEN ? AND ?
          AND TRIM(codigo_agente) = ?
          AND TRIM(agente_contraparte) = ?
          AND LOWER(TRIM(tipo_transaccion)) = ?
          AND NOT (LOWER(TRIM(tipo_transaccion)) = 'i' AND ({CONDICION_FLEX_CNFFF_SQL}))
    """
    params = [f_ini, f_fin, ag_nac_clean, agente_contraparte.strip(), tipo_transaccion.lower().strip()]
    if pais_ag:
        sql += " AND pais = ?"
        params.append(pais_ag)
    sql += " GROUP BY fecha_operacion, UPPER(TRIM(tipo_contrato))"

    df = ejecutar_consulta_df(sql, conn, params=params)

    datos_dia = {}
    if not df.empty:
        for _, r in df.iterrows():
            f = str(r["fecha"])[:10]
            tc = str(r["tipo_contrato"] or "").upper().strip()
            datos_dia.setdefault(f, {"CF": 0.0, "CNFFF": 0.0})
            if tc.startswith("CN"):
                datos_dia[f]["CNFFF"] += float(r["mwh"] or 0.0)
            elif tc.startswith("CF") or tc != "":
                datos_dia[f]["CF"] += float(r["mwh"] or 0.0)

    filas = []
    for dia in range(1, ultimo_dia + 1):
        f_iso = f"{anio:04d}-{mes:02d}-{dia:02d}"
        f_etiqueta = f"{dia}-{MESES_CORTOS_ES[mes]}-{str(anio)[2:]}"
        cf_val = datos_dia.get(f_iso, {}).get("CF", 0.0)
        cnfff_val = datos_dia.get(f_iso, {}).get("CNFFF", 0.0)
        filas.append({
            "fecha_iso": f_iso,
            "Fecha": f_etiqueta,
            "CF_MWh": cf_val,
            "CNFFF_MWh": cnfff_val,
            "Total_MWh": cf_val + cnfff_val,
        })

    return pd.DataFrame(filas)


def generar_pdf_reporte_cnd(
    conn,
    anio: int,
    mes: int,
    agente_nacional: str,
    agente_contraparte: str,
    nombre_suministrador: str,
    nombre_receptor: str = "ENEE",
    tipo_transaccion: str = "r",
    ruta_logo: str = "logo_cnd.png",
    ruta_salida: str = None,
    elaborado_por: str = "",
):
    """Genera el reporte mensual oficial en PDF estilo CND con logo, tabla CF / CNFFF y espacio para firma."""
    df_mes = construir_df_mensual_cnd(conn, anio, mes, agente_nacional, agente_contraparte, tipo_transaccion)

    if not ruta_salida:
        sumin_limpio = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in nombre_suministrador.strip())
        ruta_salida = f"Reporte_CND_{sumin_limpio}_{anio:04d}_{mes:02d}.pdf"

    def fmt_num(val):
        return f"{val:,.3f}" if val > 0.0004 else "-"

    st_titulo = ParagraphStyle(
        "TituloCND", fontName="Helvetica-Bold", fontSize=11, leading=14, alignment=TA_CENTER, textColor=colors.black
    )
    st_hdr = ParagraphStyle(
        "HdrCND", fontName="Helvetica-Bold", fontSize=9.5, leading=11.5, alignment=TA_CENTER, textColor=colors.black
    )

    if os.path.exists(ruta_logo):
        celda_logo = Image(ruta_logo, width=3.8 * cm, height=1.35 * cm)
    else:
        celda_logo = Paragraph(
            "<b>CENTRO NACIONAL<br/>DE DESPACHO</b>",
            ParagraphStyle("LogoFallback", fontName="Helvetica-Bold", fontSize=8.5, leading=10, alignment=TA_CENTER)
        )

    titulo_texto = Paragraph(
        f"Energía Suministrada por {nombre_suministrador} a {nombre_receptor}",
        st_titulo
    )

    filas = [
        [celda_logo, titulo_texto, "", ""],
        [
            Paragraph("Fecha", st_hdr),
            Paragraph("Origen Mercado<br/>Eléctrico Regional CF", st_hdr),
            Paragraph("Origen Mercado<br/>Eléctrico Regional<br/>CNFFF", st_hdr),
            Paragraph("Total", st_hdr),
        ],
        ["", "MWh", "MWh", "MWh"],
    ]

    for _, r in df_mes.iterrows():
        filas.append([
            r["Fecha"],
            fmt_num(r["CF_MWh"]),
            fmt_num(r["CNFFF_MWh"]),
            f"{r['Total_MWh']:,.3f}",
        ])

    tot_cf = float(df_mes["CF_MWh"].sum())
    tot_cnfff = float(df_mes["CNFFF_MWh"].sum())
    tot_general = tot_cf + tot_cnfff

    filas.append([
        "Total MWh",
        fmt_num(tot_cf),
        fmt_num(tot_cnfff),
        fmt_num(tot_general),
    ])

    color_dorado = colors.HexColor("#B5891A")
    ancho_cols = [4.3 * cm, 4.4 * cm, 4.4 * cm, 3.6 * cm]

    tabla = Table(filas, colWidths=ancho_cols, repeatRows=3)
    tabla.setStyle(TableStyle([
        ("SPAN", (1, 0), (3, 0)),
        ("SPAN", (0, 1), (0, 2)),

        ("BACKGROUND", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (1, 0), (3, 0), color_dorado),
        ("BACKGROUND", (0, 1), (3, 2), color_dorado),
        ("BACKGROUND", (0, -1), (3, -1), color_dorado),

        ("FONTNAME", (0, 2), (3, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 2), (3, 2), 9.5),
        ("ALIGN", (0, 0), (3, 2), "CENTER"),
        ("VALIGN", (0, 0), (3, -1), "MIDDLE"),

        ("FONTNAME", (0, 3), (3, -2), "Helvetica"),
        ("FONTSIZE", (0, 3), (3, -2), 8.8),
        ("ALIGN", (0, 3), (3, -2), "RIGHT"),
        ("TOPPADDING", (0, 3), (3, -2), 1.75),
        ("BOTTOMPADDING", (0, 3), (3, -2), 1.75),

        ("FONTNAME", (0, -1), (3, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (3, -1), 9.5),
        ("ALIGN", (0, -1), (0, -1), "CENTER"),
        ("ALIGN", (1, -1), (3, -1), "RIGHT"),

        ("BOX", (0, 0), (-1, -1), 1.8, colors.black),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
        ("LINEBELOW", (0, 2), (-1, 2), 1.2, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.black),
    ]))

    elementos = [tabla]

    nombre_firma = str(elaborado_por or "").strip()
    if nombre_firma:
        st_firma = ParagraphStyle(
            "FirmaCND",
            fontName="Helvetica",
            fontSize=9.0,
            leading=12.5,
            alignment=TA_LEFT,
            textColor=colors.black,
        )
        bloque_firma = Paragraph(
            f"____________________________________<br/>"
            f"<b>Elaborado por:</b> {nombre_firma}<br/>"
            f"Departamento Liquidación de Transacciones de Mercado",
            st_firma,
        )
        elementos.append(Spacer(1, 2.0 * cm))
        elementos.append(bloque_firma)

    doc = SimpleDocTemplate(
        ruta_salida,
        pagesize=letter,
        topMargin=0.9 * cm,
        bottomMargin=0.9 * cm,
        leftMargin=2.2 * cm,
        rightMargin=2.2 * cm,
    )
    doc.build(elementos)
    return ruta_salida


def exportar_reporte_mer(fecha_inicio, fecha_fin, pais_filtro=None):
    conn = obtener_conexion()
    etiqueta = f"_{pais_filtro}" if pais_filtro else "_REGIONAL"
    archivo_salida = f"Reporte_MER_{fecha_inicio}_al_{fecha_fin}{etiqueta}.xlsx"

    df_bal = construir_df_balance_horario(conn, fecha_inicio, fecha_fin, pais_filtro)
    df_bal_xlsx = pd.DataFrame({
        "FECHA": df_bal["fecha"].apply(formatear_fecha_corta_es),
        "HORA": df_bal["hora"] + ":00",
        "RETIROS CONTRATOS (MWh)": df_bal["ret_contratos"],
        "RETIROS OPORTUNIDAD (MWh)": df_bal["ret_oportunidad"],
        "TOTAL COMPRAS (MWh)": df_bal["ret_contratos"] + df_bal["ret_oportunidad"],
        "INYECCIONES CONTRATOS (MWh)": df_bal["iny_contratos"],
        "INYECCIONES OPORTUNIDAD (MWh)": df_bal["iny_oportunidad"],
        "TOTAL VENTAS (MWh)": df_bal["iny_contratos"] + df_bal["iny_oportunidad"],
    })

    df_tcp_ag = construir_df_tcp_agente_contraparte(conn, fecha_inicio, fecha_fin, pais_filtro)
    df_top_ag = construir_df_top_agente_hora(conn, fecha_inicio, fecha_fin, pais_filtro)

    df_pex = ejecutar_consulta_df("""
        SELECT fecha_operacion AS fecha, strftime('%H', fecha_hora) || ':00' AS hora,
               codigo_nodo AS nodo, precio_exante_usd AS precio_exante_usd
        FROM fact_precios_exante
        WHERE fecha_operacion BETWEEN ? AND ?
        ORDER BY fecha, hora, nodo
    """, conn, params=[fecha_inicio, fecha_fin])

    if not df_pex.empty:
        df_pex = df_pex.rename(columns={
            "fecha": "Fecha",
            "hora": "Hora",
            "nodo": "Nodo",
            "precio_exante_usd": "Precio_ExAnte_USD",
        })
        if pais_filtro:
            df_pex["Pais"] = df_pex["Nodo"].astype(str).str[0].map(PREFIJO_PAIS).fillna("OTRO")
            df_pex = df_pex[df_pex["Pais"] == pais_filtro].drop(columns=["Pais"])

    with pd.ExcelWriter(archivo_salida, engine="openpyxl") as writer:
        df_bal_xlsx.to_excel(writer, sheet_name="Balance_Horario", index=False)
        if not df_tcp_ag.empty:
            df_tcp_ag.to_excel(writer, sheet_name="TCP_Agente_Contraparte", index=False)
        if not df_top_ag.empty:
            df_top_ag.to_excel(writer, sheet_name="TOP_Agente_Hora", index=False)
        if not df_pex.empty:
            df_pex.to_excel(writer, sheet_name="Precios_ExAnte", index=False)

    conn.close()
    return archivo_salida
