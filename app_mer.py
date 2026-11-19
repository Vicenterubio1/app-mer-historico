import streamlit as st
import pandas as pd
import os
import io
import tempfile
import contextlib
from datetime import datetime

from db_mer import (
    obtener_conexion,
    ejecutar_consulta_df,
    ejecutar_sql,
    es_postgres,
    ESQUEMA_PG,
)
from cargar_mer import procesar_carpeta, inicializar_bd
from consultar_mer import (
    exportar_reporte_mer,
    construir_df_balance_horario,
    construir_df_tcp_agente_contraparte,
    construir_df_top_agente_hora,
    construir_df_mensual_cnd,
    generar_pdf_reporte_cnd,
    obtener_diccionario_agentes,
    formatear_fecha_corta_es,
    PREFIJO_PAIS,
)
from reporte_compra_venta_pdf import generar_pdf_informe_compra_venta

st.set_page_config(page_title="Gestión Histórica MER", page_icon="⚡", layout="wide")


def limpiar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


# ==========================================
# FUNCIONES DE CATÁLOGO MAESTRO
# ==========================================
def inicializar_y_sincronizar_catalogo(conn):
    """Puebla tablas de catálogo con códigos nuevos detectados en la BD (compatible SQLite y PostgreSQL)."""
    cur = conn.cursor()
    ejecutar_sql(
        cur,
        """
        CREATE TABLE IF NOT EXISTS dim_catalogo_nodos (
            codigo_nodo TEXT PRIMARY KEY,
            pais TEXT,
            nombre_nodo TEXT DEFAULT '',
            subestacion TEXT DEFAULT '',
            nivel_kv TEXT DEFAULT ''
        )
        """,
    )
    ejecutar_sql(
        cur,
        """
        CREATE TABLE IF NOT EXISTS dim_catalogo_agentes (
            codigo_agente TEXT PRIMARY KEY,
            pais TEXT,
            nombre_agente TEXT DEFAULT '',
            tipo_agente TEXT DEFAULT ''
        )
        """,
    )
    conn.commit()

    nodos_bd = ejecutar_consulta_df(
        "SELECT DISTINCT codigo_nodo AS cod FROM fact_precios_exante WHERE codigo_nodo IS NOT NULL",
        conn,
    )
    if not nodos_bd.empty:
        for cod in nodos_bd["cod"]:
            cod_clean = limpiar_codigo(cod)
            if cod_clean:
                pais_est = PREFIJO_PAIS.get(cod_clean[0], "OTRO")
                ejecutar_sql(
                    cur,
                    "INSERT INTO dim_catalogo_nodos (codigo_nodo, pais) VALUES (?, ?) ON CONFLICT (codigo_nodo) DO NOTHING",
                    (cod_clean, pais_est),
                )

    agentes_bd = ejecutar_consulta_df(
        """
        SELECT DISTINCT TRIM(codigo_agente) AS cod, pais FROM fact_oportunidad_top WHERE codigo_agente IS NOT NULL
        UNION
        SELECT DISTINCT TRIM(codigo_agente) AS cod, pais FROM fact_contratos_tcp WHERE codigo_agente IS NOT NULL
        UNION
        SELECT DISTINCT TRIM(agente_contraparte) AS cod,
               CASE SUBSTR(TRIM(agente_contraparte), 1, 1)
                   WHEN '1' THEN 'GT' WHEN '2' THEN 'SV' WHEN '3' THEN 'HN'
                   WHEN '4' THEN 'NI' WHEN '5' THEN 'CR' WHEN '6' THEN 'PA'
                   ELSE 'OTRO'
               END AS pais
        FROM fact_contratos_tcp WHERE agente_contraparte IS NOT NULL AND TRIM(agente_contraparte) != ''
        """,
        conn,
    )
    if not agentes_bd.empty:
        for _, row in agentes_bd.iterrows():
            cod_clean = limpiar_codigo(row["cod"])
            if cod_clean:
                ejecutar_sql(
                    cur,
                    "INSERT INTO dim_catalogo_agentes (codigo_agente, pais) VALUES (?, ?) ON CONFLICT (codigo_agente) DO NOTHING",
                    (cod_clean, row["pais"]),
                )
    conn.commit()
    cur.close()


def obtener_mapa_etiquetas_nodos(conn):
    df_cat = ejecutar_consulta_df("SELECT codigo_nodo, pais, nombre_nodo FROM dim_catalogo_nodos", conn)
    mapa = {}
    for _, r in df_cat.iterrows():
        cod = limpiar_codigo(r["codigo_nodo"])
        nom = str(r["nombre_nodo"] or "").strip()
        pais = str(r["pais"] or "").strip()
        if nom:
            mapa[cod] = f"{cod} - {nom} ({pais})"
        else:
            mapa[cod] = f"{cod} ({pais})"
    return mapa


# Inicializar BD y sincronizar catálogo al arrancar
conn_init = obtener_conexion()
inicializar_bd(conn_init)
inicializar_y_sincronizar_catalogo(conn_init)
conn_init.close()

# ==========================================
# ENCABEZADO Y BARRA LATERAL
# ==========================================
st.title("⚡ Sistema Histórico del Mercado Eléctrico Regional (MER)")
st.caption("Ingesta de Predespachos (PUB004), Control de Versiones, Catálogo y Reportería Horaria / CND")

conn = obtener_conexion()
if es_postgres(conn):
    st.sidebar.success(f"☁️ Conectado a Supabase (Esquema `{ESQUEMA_PG}`)")
else:
    st.sidebar.info("💻 Conectado a SQLite Local")

st.sidebar.header("🔍 Filtros de Consulta")

rango_df = ejecutar_consulta_df(
    "SELECT MIN(fecha_operacion) AS min_f, MAX(fecha_operacion) AS max_f FROM dim_archivos_cargados",
    conn,
)
tiene_datos = pd.notna(rango_df["min_f"][0])

if tiene_datos:
    fechas_disp = ejecutar_consulta_df(
        "SELECT DISTINCT fecha_operacion FROM dim_archivos_cargados ORDER BY fecha_operacion DESC",
        conn,
    )["fecha_operacion"].tolist()

    modo_fecha = st.sidebar.radio(
        "Modo de Selección de Fecha:",
        options=["Un solo día", "Rango de fechas"],
        horizontal=True,
    )

    if modo_fecha == "Un solo día":
        dia_sel = st.sidebar.selectbox("Fecha Operativa:", options=fechas_disp, index=0)
        f_ini = dia_sel
        f_fin = dia_sel
    else:
        min_date = datetime.strptime(str(rango_df["min_f"][0])[:10], "%Y-%m-%d").date()
        max_date = datetime.strptime(str(rango_df["max_f"][0])[:10], "%Y-%m-%d").date()
        fechas_sel = st.sidebar.date_input(
            "Rango de Fechas Operativas:",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        f_ini = str(fechas_sel[0]) if isinstance(fechas_sel, tuple) and len(fechas_sel) > 0 else str(min_date)
        f_fin = str(fechas_sel[1]) if isinstance(fechas_sel, tuple) and len(fechas_sel) > 1 else f_ini

    pais_opcion = st.sidebar.selectbox(
        "País / Área de Control:",
        options=["TODOS (Regional)", "HN - Honduras", "GT - Guatemala", "SV - El Salvador", "NI - Nicaragua", "CR - Costa Rica", "PA - Panamá"],
    )
    pais_cod = None if pais_opcion.startswith("TODOS") else pais_opcion.split(" - ")[0]
else:
    st.sidebar.info("Aún no hay datos cargados. Sube tus primeros archivos ZIP en la pestaña 'Ingreso de Archivos'.")
    f_ini, f_fin, pais_cod = None, None, None

# ==========================================
# PESTAÑAS PRINCIPALES
# ==========================================
tab_reportes, tab_catalogo, tab_ingesta, tab_auditoria = st.tabs([
    "📊 Reportería y Balance MER",
    "🏷️ Catálogo de Nodos y Agentes",
    "📥 Ingreso de Archivos (ZIP)",
    "🗂️ Auditoría de Base de Datos",
])

# ------------------------------------------
# PESTAÑA 1: REPORTERÍA Y VISUALIZACIÓN
# ------------------------------------------
with tab_reportes:
    if not tiene_datos:
        st.warning("No hay registros disponibles para mostrar. Ve a la pestaña 'Ingreso de Archivos (ZIP)'.")
    else:
        mapa_nodos = obtener_mapa_etiquetas_nodos(conn)

        df_pex = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha, strftime('%H', fecha_hora) AS hora,
                   codigo_nodo AS nodo_raw, precio_exante_usd AS precio_usd
            FROM fact_precios_exante
            WHERE fecha_operacion BETWEEN ? AND ?
            """,
            conn,
            params=[f_ini, f_fin],
        )

        if not df_pex.empty:
            df_pex = df_pex.rename(columns={
                "fecha": "Fecha",
                "hora": "Hora",
                "nodo_raw": "Nodo_Raw",
                "precio_usd": "Precio_USD",
            })
            df_pex["Nodo"] = df_pex["Nodo_Raw"].apply(limpiar_codigo)
            df_pex["Pais"] = df_pex["Nodo"].str[0].map(PREFIJO_PAIS).fillna("OTRO")
            if pais_cod:
                df_pex = df_pex[df_pex["Pais"] == pais_cod]
            df_pex["Etiqueta_Nodo"] = df_pex["Nodo"].map(mapa_nodos).fillna(df_pex["Nodo"])

        params_filtro = [f_ini, f_fin]
        cond_pais = ""
        if pais_cod:
            cond_pais = " AND pais = ?"
            params_filtro.append(pais_cod)

        df_top_res = ejecutar_consulta_df(
            f"""
            SELECT LOWER(TRIM(tipo_transaccion)) AS tipo,
                   SUM(mw_declarada) AS declarada,
                   SUM(mw_predespachados) AS predespachada
            FROM fact_oportunidad_top
            WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
            GROUP BY LOWER(TRIM(tipo_transaccion))
            """,
            conn,
            params=params_filtro,
        )

        df_tcp_res = ejecutar_consulta_df(
            f"""
            SELECT LOWER(TRIM(tipo_transaccion)) AS tipo,
                   SUM(mw_declarada) AS declarada,
                   SUM(mw_predespachados) AS predespachada
            FROM fact_contratos_tcp
            WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
            GROUP BY LOWER(TRIM(tipo_transaccion))
            """,
            conn,
            params=params_filtro,
        )

        def extraer_val(df, tipo, col):
            fila = df[df["tipo"] == tipo]
            return float(fila[col].iloc[0]) if not fila.empty and pd.notna(fila[col].iloc[0]) else 0.0

        tcp_iny_pred = extraer_val(df_tcp_res, "i", "predespachada")
        tcp_iny_decl = extraer_val(df_tcp_res, "i", "declarada")
        tcp_ret_pred = extraer_val(df_tcp_res, "r", "predespachada")
        tcp_ret_decl = extraer_val(df_tcp_res, "r", "declarada")

        top_iny_pred = extraer_val(df_top_res, "i", "predespachada")
        top_ret_pred = extraer_val(df_top_res, "r", "predespachada")

        st.markdown("#### 📌 Indicadores de Precios y Despacho Predespachado")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Precio Ex-Ante Promedio", f"${df_pex['Precio_USD'].mean():.2f} /MWh" if not df_pex.empty else "$0.00")
        c2.metric("Precio Ex-Ante Máximo", f"${df_pex['Precio_USD'].max():.2f} /MWh" if not df_pex.empty else "$0.00")
        c3.metric(
            "Contratos TCP - Ventas (Iny. 'i')",
            f"{tcp_iny_pred:,.3f} MWh",
            delta=f"Declarada: {tcp_iny_decl:,.3f} MWh",
            delta_color="off",
        )
        c4.metric(
            "Contratos TCP - Compras (Ret. 'r')",
            f"{tcp_ret_pred:,.3f} MWh",
            delta=f"Declarada: {tcp_ret_decl:,.3f} MWh",
            delta_color="off",
        )

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Oportunidad TOP - Ventas (Iny. 'i')", f"{top_iny_pred:,.3f} MWh")
        c6.metric("Oportunidad TOP - Compras (Ret. 'r')", f"{top_ret_pred:,.3f} MWh")
        total_ventas = tcp_iny_pred + top_iny_pred
        total_compras = tcp_ret_pred + top_ret_pred
        c7.metric("Total Exportación / Ventas (TCP+TOP)", f"{total_ventas:,.3f} MWh")
        c8.metric("Intercambio Neto (Ventas - Compras)", f"{(total_ventas - total_compras):+,.3f} MWh")

        st.divider()

        # ==========================================
        # SECCIÓN: REPORTE MENSUAL OFICIAL CND (PDF CON LOGO)
        # ==========================================
        st.subheader("📄 Reporte Mensual Oficial CND (Impresión PDF con Logo)")
        st.caption("Genera el cuadro diario mensual de energía suministrada por contrato (CF / CNFFF) entre un Agente Nacional y su Contraparte, listo para imprimir en PDF.")

        pares_tcp = ejecutar_consulta_df(
            """
            SELECT DISTINCT TRIM(codigo_agente) AS agente_nac,
                            TRIM(agente_contraparte) AS contraparte
            FROM fact_contratos_tcp
            WHERE TRIM(codigo_agente) != '' AND TRIM(agente_contraparte) != ''
            ORDER BY agente_nac, contraparte
            """,
            conn,
        )

        if pares_tcp.empty:
            st.info("Aún no hay registros con Agente Contraparte en `fact_contratos_tcp` para generar el reporte mensual CND.")
        else:
            mapa_agentes = obtener_diccionario_agentes(conn)
            meses_disponibles = sorted(list({str(f)[:7] for f in fechas_disp}), reverse=True)

            col_cnd1, col_cnd2, col_cnd3, col_cnd4 = st.columns([1.1, 1.4, 1.4, 1.1])
            with col_cnd1:
                mes_sel = st.selectbox("Mes del Reporte (AAAA-MM):", options=meses_disponibles, key="cnd_mes")
                anio_cnd = int(mes_sel.split("-")[0])
                mes_num_cnd = int(mes_sel.split("-")[1])

            with col_cnd2:
                lista_ag_nac = sorted(pares_tcp["agente_nac"].unique().tolist())

                def fmt_ag(cod):
                    nom = mapa_agentes.get(cod, "")
                    return f"{cod} - {nom}" if nom else cod

                ag_nac_sel = st.selectbox(
                    "Agente Nacional (Receptor):",
                    options=lista_ag_nac,
                    format_func=fmt_ag,
                    key="cnd_ag_nac",
                )

            with col_cnd3:
                lista_cp = sorted(
                    pares_tcp[pares_tcp["agente_nac"] == ag_nac_sel]["contraparte"].unique().tolist()
                )
                cp_sel = st.selectbox(
                    "Agente Contraparte (Suministrador):",
                    options=lista_cp,
                    format_func=fmt_ag,
                    key="cnd_cp",
                )

            with col_cnd4:
                sentido_cnd = st.selectbox(
                    "Sentido:",
                    options=["r - Compra / Suministro Recibido", "i - Venta / Suministro Entregado"],
                    key="cnd_sentido",
                )
                tipo_tr_cnd = sentido_cnd.split(" - ")[0]

            col_nom1, col_nom2 = st.columns(2)
            nom_default_sumin = mapa_agentes.get(cp_sel) or cp_sel
            nom_default_recep = mapa_agentes.get(ag_nac_sel) or ("ENEE" if "ENEE" in ag_nac_sel else ag_nac_sel)

            with col_nom1:
                nombre_sumin_pdf = st.text_input(
                    "Nombre del Suministrador en el Título PDF:",
                    value=nom_default_sumin,
                    key=f"txt_sumin_{cp_sel}",
                )
            with col_nom2:
                nombre_recep_pdf = st.text_input(
                    "Nombre del Receptor en el Título PDF:",
                    value=nom_default_recep,
                    key=f"txt_recep_{ag_nac_sel}",
                )

            df_preview_cnd = construir_df_mensual_cnd(
                conn, anio_cnd, mes_num_cnd, ag_nac_sel, cp_sel, tipo_tr_cnd
            )

            col_prev_tbl, col_prev_acc = st.columns([2.3, 1])
            with col_prev_tbl:
                df_prev_show = df_preview_cnd[["Fecha", "CF_MWh", "CNFFF_MWh", "Total_MWh"]].copy()
                fila_tot_cnd = pd.DataFrame([{
                    "Fecha": "Total MWh",
                    "CF_MWh": df_prev_show["CF_MWh"].sum(),
                    "CNFFF_MWh": df_prev_show["CNFFF_MWh"].sum(),
                    "Total_MWh": df_prev_show["Total_MWh"].sum(),
                }])
                df_prev_show = pd.concat([df_prev_show, fila_tot_cnd], ignore_index=True)

                def fmt_dash(v):
                    return f"{v:,.3f}" if v > 0.0004 else "-"

                st.dataframe(
                    df_prev_show.style.format({
                        "CF_MWh": fmt_dash,
                        "CNFFF_MWh": fmt_dash,
                        "Total_MWh": "{:,.3f}",
                    }),
                    use_container_width=True,
                    hide_index=True,
                    height=310,
                )

            with col_prev_acc:
                st.markdown("**🖨️ Exportar a PDF Institucional**")
                if os.path.exists("logo_cnd.png"):
                    st.success("Logo `logo_cnd.png` detectado en el repositorio.")
                else:
                    st.info("Tip: Sube el logo como `logo_cnd.png` para incluirlo en la esquina superior izquierda.")

                if st.button("📄 Generar PDF Oficial CND", type="primary", use_container_width=True):
                    ruta_pdf = generar_pdf_reporte_cnd(
                        conn=conn,
                        anio=anio_cnd,
                        mes=mes_num_cnd,
                        agente_nacional=ag_nac_sel,
                        agente_contraparte=cp_sel,
                        nombre_suministrador=nombre_sumin_pdf,
                        nombre_receptor=nombre_recep_pdf,
                        tipo_transaccion=tipo_tr_cnd,
                        ruta_logo="logo_cnd.png",
                    )
                    if os.path.exists(ruta_pdf):
                        with open(ruta_pdf, "rb") as f_pdf:
                            st.download_button(
                                label=f"⬇️ Descargar {os.path.basename(ruta_pdf)}",
                                data=f_pdf.read(),
                                file_name=os.path.basename(ruta_pdf),
                                mime="application/pdf",
                                use_container_width=True,
                            )

        st.divider()

        # ==========================================
        # TABLA 1: BALANCE HORARIO DE RETIROS E INYECCIONES (3 DECIMALES)
        # ==========================================
        st.subheader(f"📋 Balance Horario de Transacciones ({pais_cod or 'REGIONAL MER'})")
        df_bal_web = construir_df_balance_horario(conn, f_ini, f_fin, pais_cod)

        df_tabla_web = pd.DataFrame({
            "FECHA": df_bal_web["fecha"].apply(formatear_fecha_corta_es),
            "HORA": df_bal_web["hora"] + ":00",
            "RETIROS CONTRATOS (MWh)": df_bal_web["ret_contratos"],
            "RETIROS OPORTUNIDAD (MWh)": df_bal_web["ret_oportunidad"],
            "TOTAL COMPRAS (MWh)": df_bal_web["ret_contratos"] + df_bal_web["ret_oportunidad"],
            "INYECCIONES CONTRATOS (MWh)": df_bal_web["iny_contratos"],
            "INYECCIONES OPORTUNIDAD (MWh)": df_bal_web["iny_oportunidad"],
            "TOTAL VENTAS (MWh)": df_bal_web["iny_contratos"] + df_bal_web["iny_oportunidad"],
        })

        fila_tot_web = pd.DataFrame([{
            "FECHA": "TOTAL",
            "HORA": "",
            "RETIROS CONTRATOS (MWh)": df_tabla_web["RETIROS CONTRATOS (MWh)"].sum(),
            "RETIROS OPORTUNIDAD (MWh)": df_tabla_web["RETIROS OPORTUNIDAD (MWh)"].sum(),
            "TOTAL COMPRAS (MWh)": df_tabla_web["TOTAL COMPRAS (MWh)"].sum(),
            "INYECCIONES CONTRATOS (MWh)": df_tabla_web["INYECCIONES CONTRATOS (MWh)"].sum(),
            "INYECCIONES OPORTUNIDAD (MWh)": df_tabla_web["INYECCIONES OPORTUNIDAD (MWh)"].sum(),
            "TOTAL VENTAS (MWh)": df_tabla_web["TOTAL VENTAS (MWh)"].sum(),
        }])
        df_tabla_web = pd.concat([df_tabla_web, fila_tot_web], ignore_index=True)

        cols_mwh = [c for c in df_tabla_web.columns if "(MWh)" in c]
        st.dataframe(
            df_tabla_web.style.format({c: "{:,.3f}" for c in cols_mwh}),
            use_container_width=True,
            hide_index=True,
        )

        col_cv1, col_cv2 = st.columns([1.2, 2.8])
        with col_cv1:
            if st.button("📄 Generar PDF Compra y Venta Regional", type="primary", use_container_width=True, key="btn_pdf_compra_venta"):
                ruta_pdf_cv = generar_pdf_informe_compra_venta(
                    conn=conn,
                    fecha_inicio=f_ini,
                    fecha_fin=f_fin,
                    pais_filtro=pais_cod,
                    ruta_logo="logo_cnd.png",
                )
                if os.path.exists(ruta_pdf_cv):
                    with open(ruta_pdf_cv, "rb") as f_cv:
                        st.session_state["pdf_cv_bytes"] = f_cv.read()
                        st.session_state["pdf_cv_nombre"] = os.path.basename(ruta_pdf_cv)

        with col_cv2:
            if "pdf_cv_bytes" in st.session_state:
                st.download_button(
                    label=f"⬇️ Descargar {st.session_state['pdf_cv_nombre']}",
                    data=st.session_state["pdf_cv_bytes"],
                    file_name=st.session_state["pdf_cv_nombre"],
                    mime="application/pdf",
                    key="dl_pdf_compra_venta",
                )

        st.divider()

        # ==========================================
        # TABLA 2: REPORTERÍA HORARIA POR AGENTE Y CONTRAPARTE (TCP Y TOP)
        # ==========================================
        st.subheader(f"🤝 Reportería Horaria por Agente y Contraparte ({pais_cod or 'REGIONAL MER'})")
        tab_ag_tcp_hora, tab_ag_top_hora = st.tabs([
            "📄 Contratos (TCP) - Agente y Contraparte por Hora",
            "⚡ Oportunidad (TOP) - Agente por Hora",
        ])

        with tab_ag_tcp_hora:
            df_tcp_ag = construir_df_tcp_agente_contraparte(conn, f_ini, f_fin, pais_cod)
            if not df_tcp_ag.empty:
                f_col1, f_col2, f_col3 = st.columns(3)
                with f_col1:
                    agentes_tcp_opts = ["TODOS"] + sorted(df_tcp_ag["Agente_Nacional"].unique().tolist())
                    sel_ag_tcp = st.selectbox("Filtrar por Agente Nacional (Col. D):", options=agentes_tcp_opts, key="f_ag_tcp")
                with f_col2:
                    contrapartes_opts = ["TODAS"] + sorted(df_tcp_ag["Agente_Contraparte"].unique().tolist())
                    sel_cp_tcp = st.selectbox("Filtrar por Agente Contraparte (Col. I):", options=contrapartes_opts, key="f_cp_tcp")
                with f_col3:
                    op_tcp_opts = ["TODAS"] + sorted(df_tcp_ag["Operacion"].unique().tolist())
                    sel_op_tcp = st.selectbox("Tipo de Operación:", options=op_tcp_opts, key="f_op_tcp")

                df_tcp_filtrado = df_tcp_ag.copy()
                if sel_ag_tcp != "TODOS":
                    df_tcp_filtrado = df_tcp_filtrado[df_tcp_filtrado["Agente_Nacional"] == sel_ag_tcp]
                if sel_cp_tcp != "TODAS":
                    df_tcp_filtrado = df_tcp_filtrado[df_tcp_filtrado["Agente_Contraparte"] == sel_cp_tcp]
                if sel_op_tcp != "TODAS":
                    df_tcp_filtrado = df_tcp_filtrado[df_tcp_filtrado["Operacion"] == sel_op_tcp]

                if not df_tcp_filtrado.empty:
                    fila_tot_tcp = pd.DataFrame([{
                        "Fecha": "TOTAL",
                        "Hora": "",
                        "Pais": "",
                        "Agente_Nacional": "",
                        "Nombre_Agente": "",
                        "Agente_Contraparte": "",
                        "Nombre_Contraparte": "",
                        "Tipo_Contrato": "",
                        "Operacion": "",
                        "MWh_Requeridos": df_tcp_filtrado["MWh_Requeridos"].sum(),
                        "MWh_Declarados": df_tcp_filtrado["MWh_Declarados"].sum(),
                        "Precio_ExAnte_USD": df_tcp_filtrado["Precio_ExAnte_USD"].mean(),
                        "MWh_Predespachados": df_tcp_filtrado["MWh_Predespachados"].sum(),
                    }])
                    df_tcp_mostrar = pd.concat([df_tcp_filtrado, fila_tot_tcp], ignore_index=True)

                    st.dataframe(
                        df_tcp_mostrar.style.format({
                            "MWh_Requeridos": "{:,.3f}",
                            "MWh_Declarados": "{:,.3f}",
                            "Precio_ExAnte_USD": "${:,.2f}",
                            "MWh_Predespachados": "{:,.3f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.info("No hay contratos con esa combinación de filtros.")
            else:
                st.info("No hay registros de contratos (TCP) para la fecha y país seleccionados.")

        with tab_ag_top_hora:
            df_top_ag = construir_df_top_agente_hora(conn, f_ini, f_fin, pais_cod)
            if not df_top_ag.empty:
                t_col1, t_col2 = st.columns(2)
                with t_col1:
                    agentes_top_opts = ["TODOS"] + sorted(df_top_ag["Agente_Nacional"].unique().tolist())
                    sel_ag_top = st.selectbox("Filtrar por Agente Nacional (Col. D):", options=agentes_top_opts, key="f_ag_top")
                with t_col2:
                    op_top_opts = ["TODAS"] + sorted(df_top_ag["Operacion"].unique().tolist())
                    sel_op_top = st.selectbox("Tipo de Operación:", options=op_top_opts, key="f_op_top")

                df_top_filtrado = df_top_ag.copy()
                if sel_ag_top != "TODOS":
                    df_top_filtrado = df_top_filtrado[df_top_filtrado["Agente_Nacional"] == sel_ag_top]
                if sel_op_top != "TODAS":
                    df_top_filtrado = df_top_filtrado[df_top_filtrado["Operacion"] == sel_op_top]

                if not df_top_filtrado.empty:
                    fila_tot_top = pd.DataFrame([{
                        "Fecha": "TOTAL",
                        "Hora": "",
                        "Pais": "",
                        "Agente_Nacional": "",
                        "Nombre_Agente": "",
                        "Operacion": "",
                        "MWh_Requeridos": df_top_filtrado["MWh_Requeridos"].sum(),
                        "MWh_Declarados": df_top_filtrado["MWh_Declarados"].sum(),
                        "Precio_ExAnte_USD": df_top_filtrado["Precio_ExAnte_USD"].mean(),
                        "MWh_Predespachados": df_top_filtrado["MWh_Predespachados"].sum(),
                    }])
                    df_top_mostrar = pd.concat([df_top_filtrado, fila_tot_top], ignore_index=True)

                    st.dataframe(
                        df_top_mostrar.style.format({
                            "MWh_Requeridos": "{:,.3f}",
                            "MWh_Declarados": "{:,.3f}",
                            "Precio_ExAnte_USD": "${:,.2f}",
                            "MWh_Predespachados": "{:,.3f}",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.info("No hay transacciones de oportunidad con esa combinación de filtros.")
            else:
                st.info("No hay registros de oportunidad (TOP) para la fecha y país seleccionados.")

        st.divider()

        # ==========================================
        # CURVA HORARIA DE PRECIOS EX-ANTE
        # ==========================================
        st.subheader("Curva Horaria de Precios Ex-Ante (USD/MWh)")
        if not df_pex.empty:
            etiquetas_disp = sorted(df_pex["Etiqueta_Nodo"].unique().tolist())
            col_sel1, col_sel2 = st.columns([3, 1])
            with col_sel1:
                etiquetas_sel = st.multiselect(
                    "Selecciona uno o varios nodos (deja vacío para ver el promedio del país/región):",
                    options=etiquetas_disp,
                    default=etiquetas_disp[:3] if len(etiquetas_disp) >= 3 else etiquetas_disp,
                    key=f"ms_nodos_{pais_cod or 'ALL'}",
                )
            with col_sel2:
                incluir_prom_graf = st.checkbox("Incluir Promedio País/Región en curva", value=True)

            df_graf = df_pex[df_pex["Etiqueta_Nodo"].isin(etiquetas_sel)] if etiquetas_sel else df_pex

            if df_graf["Fecha"].nunique() > 1 and etiquetas_sel:
                df_graf = df_graf.copy()
                df_graf["Serie"] = df_graf["Fecha"] + " | " + df_graf["Etiqueta_Nodo"]
                curva = df_graf.groupby(["Hora", "Serie"])["Precio_USD"].mean().unstack()
            elif etiquetas_sel:
                curva = df_graf.groupby(["Hora", "Etiqueta_Nodo"])["Precio_USD"].mean().unstack()
            else:
                curva = df_graf.groupby(["Hora", "Fecha"])["Precio_USD"].mean().unstack()

            etiqueta_prom = f"Promedio Todos los Nodos ({pais_cod or 'MER'})"
            if incluir_prom_graf and etiquetas_sel:
                prom_horario_serie = df_pex.groupby("Hora")["Precio_USD"].mean()
                curva[etiqueta_prom] = prom_horario_serie

            st.line_chart(curva)

            with st.expander("🕒 Ver Tabla Detallada de Precios por Hora (Promedio, Mínimo, Máximo y Nodos)", expanded=False):
                resumen_hora = df_pex.groupby("Hora")["Precio_USD"].agg(
                    Nodos_Activos="count",
                    Promedio_General="mean",
                    Minimo_Nodal="min",
                    Maximo_Nodal="max",
                )
                resumen_hora["Nodos_Activos"] = df_pex.groupby("Hora")["Nodo"].nunique()

                if etiquetas_sel:
                    piv_nodos = df_pex[df_pex["Etiqueta_Nodo"].isin(etiquetas_sel)].groupby(["Hora", "Etiqueta_Nodo"])["Precio_USD"].mean().unstack()
                    tabla_horaria = pd.concat([resumen_hora, piv_nodos], axis=1)
                else:
                    tabla_horaria = resumen_hora

                tabla_horaria = tabla_horaria.reset_index().rename(columns={
                    "Hora": "Periodo (Hora)",
                    "Nodos_Activos": "Cant. Nodos",
                    "Promedio_General": f"Promedio {pais_cod or 'MER'} ($/MWh)",
                    "Minimo_Nodal": "Mínimo ($/MWh)",
                    "Maximo_Nodal": "Máximo ($/MWh)",
                })

                fila_prom = {"Periodo (Hora)": "PROMEDIO DÍA", "Cant. Nodos": int(tabla_horaria["Cant. Nodos"].mean())}
                for col_num in tabla_horaria.columns[2:]:
                    fila_prom[col_num] = tabla_horaria[col_num].mean()
                tabla_horaria = pd.concat([tabla_horaria, pd.DataFrame([fila_prom])], ignore_index=True)

                cols_moneda = [c for c in tabla_horaria.columns if c not in ["Periodo (Hora)", "Cant. Nodos"]]
                formato_cols = {c: "${:,.3f}" for c in cols_moneda}
                st.dataframe(
                    tabla_horaria.style.format(formato_cols),
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.warning(f"No se encontraron precios en PEXANTE para el filtro seleccionado ({pais_cod or 'REGIONAL'}).")

        st.divider()

        st.subheader("📥 Exportar Reporte Ejecutivo a Excel")
        col_btn1, col_btn2 = st.columns([1, 2])
        with col_btn1:
            if st.button("Generar Archivo Excel (.xlsx)", type="primary", use_container_width=True):
                with st.spinner("Construyendo libro de Excel multi-hoja..."):
                    archivo_xlsx = exportar_reporte_mer(fecha_inicio=f_ini, fecha_fin=f_fin, pais_filtro=pais_cod)
                    if os.path.exists(archivo_xlsx):
                        with open(archivo_xlsx, "rb") as f_excel:
                            st.download_button(
                                label=f"⬇️ Descargar {archivo_xlsx}",
                                data=f_excel.read(),
                                file_name=archivo_xlsx,
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True,
                            )

# ------------------------------------------
# PESTAÑA 2: CATÁLOGO EDITABLE DE NODOS Y AGENTES
# ------------------------------------------
with tab_catalogo:
    st.subheader("🏷️ Administración de Nombres Reales (Nodos y Agentes)")
    st.markdown("Los códigos se detectan automáticamente de tus archivos `PUB004` (incluyendo los Agentes Contraparte). Edita los nombres aquí para que aparezcan automáticamente en los reportes y títulos de PDF.")

    subtab_nodos, subtab_agentes, subtab_excel_cat = st.tabs([
        "🔌 Catálogo de Nodos Eléctricos",
        "🏢 Catálogo de Agentes MER",
        "📤 Importar / Exportar Catálogo Excel",
    ])

    with subtab_nodos:
        filtro_pais_cat = st.selectbox(
            "Filtrar nodos por país:",
            options=["TODOS", "HN", "GT", "SV", "NI", "CR", "PA"],
            index=1 if pais_cod == "HN" else 0,
            key="cat_pais_nodos",
        )
        q_nodos = "SELECT codigo_nodo, pais, nombre_nodo, subestacion, nivel_kv FROM dim_catalogo_nodos"
        p_nodos = []
        if filtro_pais_cat != "TODOS":
            q_nodos += " WHERE pais = ?"
            p_nodos.append(filtro_pais_cat)
        q_nodos += " ORDER BY codigo_nodo"
        df_nodos_cat = ejecutar_consulta_df(q_nodos, conn, params=p_nodos)

        edited_nodos = st.data_editor(
            df_nodos_cat,
            use_container_width=True,
            hide_index=True,
            disabled=["codigo_nodo", "pais"],
            column_config={
                "codigo_nodo": st.column_config.TextColumn("Código Nodo EOR"),
                "pais": st.column_config.TextColumn("País"),
                "nombre_nodo": st.column_config.TextColumn("Nombre Descriptivo (Ej. Amarateca 230kV)", width="large"),
                "subestacion": st.column_config.TextColumn("Subestación"),
                "nivel_kv": st.column_config.TextColumn("Tensión (kV)"),
            },
            key="editor_nodos",
        )

        if st.button("💾 Guardar Cambios en Nodos", type="primary"):
            cur = conn.cursor()
            for _, row in edited_nodos.iterrows():
                ejecutar_sql(
                    cur,
                    """
                    UPDATE dim_catalogo_nodos
                    SET nombre_nodo = ?, subestacion = ?, nivel_kv = ?
                    WHERE codigo_nodo = ?
                    """,
                    (row["nombre_nodo"] or "", row["subestacion"] or "", row["nivel_kv"] or "", row["codigo_nodo"]),
                )
            conn.commit()
            cur.close()
            st.success("✅ Catálogo de nodos actualizado correctamente.")
            st.rerun()

    with subtab_agentes:
        df_agentes_cat = ejecutar_consulta_df(
            "SELECT codigo_agente, pais, nombre_agente, tipo_agente FROM dim_catalogo_agentes ORDER BY pais, codigo_agente",
            conn,
        )
        edited_agentes = st.data_editor(
            df_agentes_cat,
            use_container_width=True,
            hide_index=True,
            disabled=["codigo_agente", "pais"],
            column_config={
                "codigo_agente": st.column_config.TextColumn("Código Agente EOR"),
                "pais": st.column_config.TextColumn("País"),
                "nombre_agente": st.column_config.TextColumn("Nombre Empresa / Planta (Ej. PAN-AM GENERATING LIMITED)", width="large"),
                "tipo_agente": st.column_config.SelectboxColumn("Tipo", options=["", "Generador", "Distribuidor", "Comercializador", "Gran Usuario"]),
            },
            key="editor_agentes",
        )

        if st.button("💾 Guardar Cambios en Agentes", type="primary"):
            cur = conn.cursor()
            for _, row in edited_agentes.iterrows():
                ejecutar_sql(
                    cur,
                    """
                    UPDATE dim_catalogo_agentes
                    SET nombre_agente = ?, tipo_agente = ?
                    WHERE codigo_agente = ?
                    """,
                    (row["nombre_agente"] or "", row["tipo_agente"] or "", row["codigo_agente"]),
                )
            conn.commit()
            cur.close()
            st.success("✅ Catálogo de agentes actualizado correctamente.")
            st.rerun()

    with subtab_excel_cat:
        st.markdown("Descarga el catálogo actual a Excel, completa los nombres cómodamente y vuelve a subirlo.")
        buffer_cat = io.BytesIO()
        with pd.ExcelWriter(buffer_cat, engine="openpyxl") as writer:
            ejecutar_consulta_df("SELECT * FROM dim_catalogo_nodos ORDER BY codigo_nodo", conn).to_excel(writer, sheet_name="Nodos", index=False)
            ejecutar_consulta_df("SELECT * FROM dim_catalogo_agentes ORDER BY pais, codigo_agente", conn).to_excel(writer, sheet_name="Agentes", index=False)

        st.download_button(
            label="⬇️ Descargar Plantilla_Catalogo_MER.xlsx",
            data=buffer_cat.getvalue(),
            file_name="Plantilla_Catalogo_MER.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        archivo_cat_up = st.file_uploader("Subir Plantilla_Catalogo_MER.xlsx completada", type=["xlsx"])
        if archivo_cat_up and st.button("⬆️ Actualizar Catálogo desde Excel"):
            xl_cat = pd.ExcelFile(archivo_cat_up)
            cur = conn.cursor()
            if "Nodos" in xl_cat.sheet_names:
                df_n_up = pd.read_excel(xl_cat, sheet_name="Nodos").fillna("")
                for _, r in df_n_up.iterrows():
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_catalogo_nodos (codigo_nodo, pais, nombre_nodo, subestacion, nivel_kv)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(codigo_nodo) DO UPDATE SET
                            nombre_nodo=excluded.nombre_nodo,
                            subestacion=excluded.subestacion,
                            nivel_kv=excluded.nivel_kv
                        """,
                        (limpiar_codigo(r["codigo_nodo"]), str(r["pais"]).strip(), str(r["nombre_nodo"]).strip(), str(r["subestacion"]).strip(), str(r["nivel_kv"]).strip()),
                    )
            if "Agentes" in xl_cat.sheet_names:
                df_a_up = pd.read_excel(xl_cat, sheet_name="Agentes").fillna("")
                for _, r in df_a_up.iterrows():
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_catalogo_agentes (codigo_agente, pais, nombre_agente, tipo_agente)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(codigo_agente) DO UPDATE SET
                            nombre_agente=excluded.nombre_agente,
                            tipo_agente=excluded.tipo_agente
                        """,
                        (limpiar_codigo(r["codigo_agente"]), str(r["pais"]).strip(), str(r["nombre_agente"]).strip(), str(r["tipo_agente"]).strip()),
                    )
            conn.commit()
            cur.close()
            st.success("✅ Catálogo importado exitosamente desde Excel.")
            st.rerun()

# ------------------------------------------
# PESTAÑA 3: INGRESO DE ARCHIVOS (ETL)
# ------------------------------------------
with tab_ingesta:
    st.subheader("Cargar Predespachos Regionales (PUB004-*.zip)")
    st.markdown("""
    Arrastra aquí los archivos `.zip` descargados del EOR. El sistema detectará automáticamente:
- **Nuevos días operativos** (`[NUEVO]`)
- **Modificaciones o redespachos** publicados más tarde (`[UPDATE]`), reemplazando las versiones obsoletas.
- **Archivos ya existentes o más antiguos** (`[SKIP]`), omitiéndolos para proteger las versiones vigentes.
    """)

    archivos_subidos = st.file_uploader(
        "Selecciona o arrastra tus archivos PUB004-PRE-*.zip",
        type=["zip"],
        accept_multiple_files=True,
    )

    if archivos_subidos:
        st.info(f"{len(archivos_subidos)} archivo(s) listo(s) para procesar.")
        if st.button("🚀 Procesar y Guardar en Base de Datos", type="primary"):
            log_buffer = io.StringIO()
            with st.spinner("Leyendo archivos Excel y actualizando base de datos..."):
                with tempfile.TemporaryDirectory() as tmpdir:
                    for up_file in archivos_subidos:
                        ruta_tmp = os.path.join(tmpdir, up_file.name)
                        with open(ruta_tmp, "wb") as f_out:
                            f_out.write(up_file.getbuffer())

                    with contextlib.redirect_stdout(log_buffer):
                        procesar_carpeta(tmpdir)

                inicializar_y_sincronizar_catalogo(conn)

            salida_proceso = log_buffer.getvalue().strip()
            st.success("✅ ¡Sincronización completada y catálogo actualizado!")
            if salida_proceso:
                st.code(salida_proceso, language="text")
            else:
                st.warning("No se encontraron archivos válidos con patrón PUB004-*.xlsx dentro del ZIP.")

# ------------------------------------------
# PESTAÑA 4: AUDITORÍA DEL HISTORIAL
# ------------------------------------------
with tab_auditoria:
    st.subheader("Estado Actual de la Base de Datos Histórica")
    if tiene_datos:
        df_resumen_dias = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha_operativa,
                   COUNT(*) AS paises_cargados,
                   GROUP_CONCAT(pais, ', ') AS lista_paises,
                   MAX(fecha_carga) AS ultima_actualizacion
            FROM dim_archivos_cargados
            GROUP BY fecha_operacion
            ORDER BY fecha_operacion DESC
            """,
            conn,
        ).rename(columns={
            "fecha_operativa": "Fecha_Operativa",
            "paises_cargados": "Paises_Cargados",
            "lista_paises": "Lista_Paises",
            "ultima_actualizacion": "Ultima_Actualizacion",
        })
        st.dataframe(df_resumen_dias, use_container_width=True, hide_index=True)

        st.markdown("#### Detalle de Archivos Oficiales Vigentes")
        df_detalle = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha, codigo_osom AS oso, pais AS pais,
                   archivo_origen AS archivo_vigente, fecha_publicacion_eor AS publicacion_eor,
                   fecha_carga AS fecha_ingesta
            FROM dim_archivos_cargados
            ORDER BY fecha_operacion DESC, codigo_osom
            """,
            conn,
        ).rename(columns={
            "fecha": "Fecha",
            "oso": "OSO",
            "pais": "Pais",
            "archivo_vigente": "Archivo_Vigente",
            "publicacion_eor": "Publicacion_EOR",
            "fecha_ingesta": "Fecha_Ingesta",
        })
        st.dataframe(df_detalle, use_container_width=True, hide_index=True)
    else:
        st.info("No hay archivos registrados en la auditoría todavía.")

conn.close()
