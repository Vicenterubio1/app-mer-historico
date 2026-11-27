import streamlit as st
import pandas as pd
import os
import io
import hmac
import tempfile
import contextlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
from reporte_anual_cnd_pdf import (
    construir_df_anual_mer,
    generar_pdf_transacciones_anuales_mer,
)
from reporte_diario_agente_pdf import (
    construir_datos_diarios_agente,
    generar_pdf_diario_agente_cnd,
)
from declaraciones_contratos import (
    evaluar_semaforo_declaracion,
    parsear_archivo_declaracion,
    validar_declaracion_contratos,
    guardar_declaracion_agente,
    obtener_envios_fecha,
    obtener_detalle_consolidado_fecha,
    generar_excel_consolidado_eor,
    generar_xml_consolidado_eor,
    ZONA_HN,
)
from catalogo_oficial_mer import DIRECTORIO_AGENTES_MER

st.set_page_config(page_title="Gestión Histórica MER", page_icon="⚡", layout="wide")


def limpiar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def limpiar_texto_catalogo(val):
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "null", "<na>"):
        return ""
    return s


def asegurar_esquema(conn):
    """Garantiza que PostgreSQL apunte al esquema configurado (mer) en cada conexión."""
    if es_postgres(conn):
        cur_sp = conn.cursor()
        cur_sp.execute(f"SET search_path TO {ESQUEMA_PG}, public")
        cur_sp.close()


def limpiar_descarga(clave_bytes, clave_nombre):
    """Elimina de session_state el archivo generado una vez que el usuario hace clic en Descargar."""
    st.session_state.pop(clave_bytes, None)
    st.session_state.pop(clave_nombre, None)


# ==========================================
# CONTROL DE ACCESO PERSONALIZADO (st.secrets)
# ==========================================
def obtener_secreto(nombre_clave):
    try:
        if nombre_clave in st.secrets:
            return str(st.secrets[nombre_clave]).strip()
    except Exception:
        pass
    return os.environ.get(nombre_clave, "").strip()


def obtener_usuarios_configurados():
    """Lee el diccionario [usuarios] desde st.secrets."""
    try:
        if "usuarios" in st.secrets:
            return dict(st.secrets["usuarios"])
    except Exception:
        pass
    return {}


def verificar_clave(clave_ingresada, clave_real):
    if not clave_real:
        return False
    return hmac.compare_digest(str(clave_ingresada), str(clave_real))


if "usuario_activo" not in st.session_state:
    st.session_state["usuario_activo"] = None


def pantalla_login():
    usuarios_cfg = obtener_usuarios_configurados()
    clave_admin_legacy = obtener_secreto("ADMIN_PASSWORD")
    clave_visitante_legacy = obtener_secreto("VISITOR_PASSWORD")

    if not usuarios_cfg and not clave_admin_legacy and not clave_visitante_legacy:
        st.session_state["usuario_activo"] = {
            "usuario": "local",
            "nombre": "Administrador Local",
            "rol": "admin",
            "agente": "",
        }
        return True

    if st.session_state["usuario_activo"] is not None:
        return True

    st.markdown("<br>", unsafe_allow_html=True)
    col_izq, col_centro, col_der = st.columns([1, 1.4, 1])
    with col_centro:
        st.title("⚡ Sistema Histórico MER")
        st.caption("Mercado Eléctrico Regional · Acceso Personalizado")
        with st.form("form_login_personal", clear_on_submit=True):
            usr_in = st.text_input("Usuario:", placeholder="Ej. vicente").strip().lower()
            pwd_in = st.text_input("Contraseña:", type="password", placeholder="Ingresa tu contraseña...")
            ingresar = st.form_submit_button("🔓 Ingresar al Sistema", type="primary", use_container_width=True)

            if ingresar:
                if usr_in and usr_in in usuarios_cfg:
                    datos_u = usuarios_cfg[usr_in]
                    pwd_real = str(datos_u.get("password", "")).strip()
                    if verificar_clave(pwd_in, pwd_real):
                        rol_u = str(datos_u.get("rol", "visitante")).strip().lower()
                        if rol_u not in ("admin", "visitante", "agente"):
                            rol_u = "visitante"
                        ag_u = str(datos_u.get("agente", datos_u.get("codigo_agente", ""))).strip().upper()
                        st.session_state["usuario_activo"] = {
                            "usuario": usr_in,
                            "nombre": str(datos_u.get("nombre", usr_in)).strip(),
                            "rol": rol_u,
                            "agente": ag_u,
                        }
                        st.rerun()
                    else:
                        st.error("Usuario o contraseña incorrectos.")
                elif not usr_in and verificar_clave(pwd_in, clave_admin_legacy):
                    st.session_state["usuario_activo"] = {
                        "usuario": "admin",
                        "nombre": "Administrador",
                        "rol": "admin",
                        "agente": "",
                    }
                    st.rerun()
                elif not usr_in and verificar_clave(pwd_in, clave_visitante_legacy):
                    st.session_state["usuario_activo"] = {
                        "usuario": "visitante",
                        "nombre": "Usuario Consulta",
                        "rol": "visitante",
                        "agente": "",
                    }
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos. Verifica tus credenciales.")
    return False


if not pantalla_login():
    st.stop()

usuario_actual = st.session_state["usuario_activo"]
es_admin = usuario_actual["rol"] == "admin"
es_agente = usuario_actual["rol"] == "agente"


# ==========================================
# FUNCIONES DE CATÁLOGO MAESTRO E INICIALIZACIÓN
# ==========================================
def inicializar_y_sincronizar_catalogo(conn):
    """Puebla tablas de catálogo con códigos nuevos detectados en la BD y el directorio oficial MER."""
    asegurar_esquema(conn)
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
    ejecutar_sql(
        cur,
        """
        CREATE TABLE IF NOT EXISTS dim_agentes_nacionales (
            codigo_agente TEXT PRIMARY KEY,
            nombre_agente TEXT,
            activo INTEGER DEFAULT 1
        )
        """,
    )
    ejecutar_sql(
        cur,
        """
        CREATE TABLE IF NOT EXISTS dim_contratos_firmes (
            codigo_contrato_firme TEXT PRIMARY KEY,
            agente_autorizado TEXT NOT NULL,
            agente_contraparte TEXT DEFAULT '',
            descripcion TEXT DEFAULT '',
            activo INTEGER DEFAULT 1
        )
        """,
    )
    conn.commit()

    ejecutar_sql(
        cur,
        """
        UPDATE dim_catalogo_agentes
        SET nombre_agente = ''
        WHERE LOWER(TRIM(COALESCE(nombre_agente, ''))) IN ('nan', 'none', 'null', '<na>')
        """,
    )
    ejecutar_sql(
        cur,
        """
        UPDATE dim_catalogo_agentes
        SET tipo_agente = ''
        WHERE LOWER(TRIM(COALESCE(tipo_agente, ''))) IN ('nan', 'none', 'null', '<na>')
        """,
    )
    ejecutar_sql(
        cur,
        """
        UPDATE dim_catalogo_nodos
        SET nombre_nodo = ''
        WHERE LOWER(TRIM(COALESCE(nombre_nodo, ''))) IN ('nan', 'none', 'null', '<na>')
        """,
    )

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

    for cod_reg, (razon_social, actividad) in DIRECTORIO_AGENTES_MER.items():
        cod_clean = limpiar_codigo(cod_reg)
        if cod_clean:
            pais_reg = PREFIJO_PAIS.get(cod_clean[0], "OTRO")
            ejecutar_sql(
                cur,
                """
                INSERT INTO dim_catalogo_agentes (codigo_agente, pais, nombre_agente, tipo_agente)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (codigo_agente) DO UPDATE SET
                    pais = COALESCE(NULLIF(dim_catalogo_agentes.pais, ''), excluded.pais),
                    nombre_agente = CASE
                        WHEN LOWER(TRIM(COALESCE(dim_catalogo_agentes.nombre_agente, ''))) IN ('', 'nan', 'none', 'null', '<na>')
                        THEN excluded.nombre_agente
                        ELSE dim_catalogo_agentes.nombre_agente
                    END,
                    tipo_agente = CASE
                        WHEN LOWER(TRIM(COALESCE(dim_catalogo_agentes.tipo_agente, ''))) IN ('', 'nan', 'none', 'null', '<na>')
                        THEN excluded.tipo_agente
                        ELSE dim_catalogo_agentes.tipo_agente
                    END
                """,
                (cod_clean, pais_reg, razon_social.strip(), actividad.strip()),
            )

    agentes_hn_cat = ejecutar_consulta_df(
        """
        SELECT codigo_agente, nombre_agente
          FROM dim_catalogo_agentes
         WHERE UPPER(TRIM(COALESCE(pais, ''))) = 'HN'
            OR UPPER(TRIM(codigo_agente)) LIKE '3%'
        """,
        conn,
    )
    if not agentes_hn_cat.empty:
        for _, row in agentes_hn_cat.iterrows():
            cod_nac = limpiar_codigo(row.get("codigo_agente")).upper()
            if cod_nac and cod_nac.startswith("3"):
                nom_nac = limpiar_texto_catalogo(row.get("nombre_agente"))
                ejecutar_sql(
                    cur,
                    """
                    INSERT INTO dim_agentes_nacionales (codigo_agente, nombre_agente, activo)
                    VALUES (?, ?, 1)
                    ON CONFLICT (codigo_agente) DO UPDATE SET
                        nombre_agente = CASE
                            WHEN TRIM(COALESCE(dim_agentes_nacionales.nombre_agente, '')) = ''
                                 AND ? != ''
                            THEN ?
                            ELSE dim_agentes_nacionales.nombre_agente
                        END
                    """,
                    (cod_nac, nom_nac, nom_nac, nom_nac),
                )

    conn.commit()
    cur.close()


@st.cache_resource
def preparar_entorno_bd():
    """Se ejecuta una sola vez al iniciar el servidor, evitando DDL en cada clic."""
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        inicializar_bd(c)
        inicializar_y_sincronizar_catalogo(c)
        return es_postgres(c)
    finally:
        c.close()


# ==========================================
# CONSULTAS CACHEADAS BLINDADAS
# ==========================================
@st.cache_data(ttl=300, show_spinner=False)
def cargar_metadatos_fechas():
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        rango_df = ejecutar_consulta_df(
            "SELECT MIN(fecha_operacion) AS min_f, MAX(fecha_operacion) AS max_f FROM dim_archivos_cargados",
            c,
        )
        tiene = pd.notna(rango_df["min_f"][0])
        if not tiene:
            return False, None, None, []
        fechas = ejecutar_consulta_df(
            "SELECT DISTINCT fecha_operacion FROM dim_archivos_cargados ORDER BY fecha_operacion DESC",
            c,
        )["fecha_operacion"].tolist()
        return True, str(rango_df["min_f"][0])[:10], str(rango_df["max_f"][0])[:10], fechas
    except Exception:
        return False, None, None, []
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_mapa_etiquetas_nodos():
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        df_cat = ejecutar_consulta_df("SELECT codigo_nodo, pais, nombre_nodo FROM dim_catalogo_nodos", c)
        mapa = {}
        for _, r in df_cat.iterrows():
            cod = limpiar_codigo(r["codigo_nodo"])
            nom = limpiar_texto_catalogo(r["nombre_nodo"])
            pais = limpiar_texto_catalogo(r["pais"])
            mapa[cod] = f"{cod} - {nom} ({pais})" if nom else f"{cod} ({pais})"
        return mapa
    except Exception:
        return {}
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_diccionario_agentes_cache():
    mapa = {
        limpiar_codigo(cod): limpiar_texto_catalogo(nom)
        for cod, (nom, _) in DIRECTORIO_AGENTES_MER.items()
    }
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        mapa_bd = obtener_diccionario_agentes(c)
        for cod, nom in mapa_bd.items():
            nom_clean = limpiar_texto_catalogo(nom)
            if nom_clean:
                mapa[limpiar_codigo(cod)] = nom_clean
        return mapa
    except Exception:
        return mapa
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_datos_indicadores_y_precios(f_ini, f_fin, pais_cod):
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        mapa_nodos = cargar_mapa_etiquetas_nodos()
        df_pex = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha, strftime('%H', fecha_hora) AS hora,
                   codigo_nodo AS nodo_raw, precio_exante_usd AS precio_usd
            FROM fact_precios_exante
            WHERE fecha_operacion BETWEEN ? AND ?
            """,
            c,
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
            c,
            params=params_filtro,
        )

        df_flex_res = ejecutar_consulta_df(
            f"""
            SELECT SUM(ABS(mw_predespachados)) AS mwh
            FROM fact_contratos_tcp
            WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
              AND UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
              AND LOWER(TRIM(COALESCE(tipo_oferta, ''))) = 'o.i. flexibilidad'
              AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
            """,
            c,
            params=params_filtro,
        )
        flex_ventas_kpi = (
            float(df_flex_res["mwh"].iloc[0] or 0.0)
            if not df_flex_res.empty and pd.notna(df_flex_res["mwh"].iloc[0])
            else 0.0
        )
        if flex_ventas_kpi > 0:
            if not df_top_res.empty and "i" in df_top_res["tipo"].values:
                df_top_res.loc[df_top_res["tipo"] == "i", "predespachada"] += flex_ventas_kpi
            else:
                df_top_res = pd.concat(
                    [df_top_res, pd.DataFrame([{"tipo": "i", "declarada": 0.0, "predespachada": flex_ventas_kpi}])],
                    ignore_index=True,
                )

        df_tcp_res = ejecutar_consulta_df(
            f"""
            SELECT LOWER(TRIM(tipo_transaccion)) AS tipo,
                   SUM(mw_declarada) AS declarada,
                   SUM(mw_predespachados) AS predespachada
            FROM fact_contratos_tcp
            WHERE date(fecha_hora) BETWEEN ? AND ? {cond_pais}
              AND NOT (
                  LOWER(TRIM(tipo_transaccion)) = 'i'
                  AND UPPER(TRIM(COALESCE(tipo_contrato, ''))) = 'CNFFF'
                  AND LOWER(TRIM(COALESCE(tipo_oferta, ''))) = 'o.i. flexibilidad'
                  AND ABS(COALESCE(mw_predespachados, 0)) > 1e-9
              )
            GROUP BY LOWER(TRIM(tipo_transaccion))
            """,
            c,
            params=params_filtro,
        )
        return df_pex, df_top_res, df_tcp_res, None
    except Exception as e:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), str(e)
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_pares_tcp():
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        return ejecutar_consulta_df(
            """
            SELECT DISTINCT TRIM(codigo_agente) AS agente_nac,
                            TRIM(agente_contraparte) AS contraparte
            FROM fact_contratos_tcp
            WHERE TRIM(codigo_agente) != '' AND TRIM(agente_contraparte) != ''
            ORDER BY agente_nac, contraparte
            """,
            c,
        )
    except Exception:
        return pd.DataFrame()
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_agentes_nacionales_activos():
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        df = ejecutar_consulta_df(
            """
            SELECT DISTINCT TRIM(codigo_agente) AS agente_nac FROM fact_contratos_tcp WHERE TRIM(codigo_agente) != ''
            UNION
            SELECT DISTINCT TRIM(codigo_agente) AS agente_nac FROM fact_oportunidad_top WHERE TRIM(codigo_agente) != ''
            ORDER BY agente_nac
            """,
            c,
        )
        return df["agente_nac"].tolist() if not df.empty else []
    except Exception:
        return []
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_datos_diarios_agente_cache(fecha_op, ag_cod):
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        return construir_datos_diarios_agente(c, fecha_op, ag_cod)
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_df_mensual_cnd_cache(anio_cnd, mes_num_cnd, ag_nac_sel, cp_sel, tipo_tr_cnd):
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        return construir_df_mensual_cnd(c, anio_cnd, mes_num_cnd, ag_nac_sel, cp_sel, tipo_tr_cnd)
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_df_anual_mer_cache(anio_sel, pais_cod, mes_hasta):
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        return construir_df_anual_mer(c, anio_sel, pais_cod, mes_hasta=mes_hasta)
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_tablas_horarias_cache(f_ini, f_fin, pais_cod):
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        df_bal = construir_df_balance_horario(c, f_ini, f_fin, pais_cod)
        df_tcp_ag = construir_df_tcp_agente_contraparte(c, f_ini, f_fin, pais_cod)
        df_top_ag = construir_df_top_agente_hora(c, f_ini, f_fin, pais_cod)
        return df_bal, df_tcp_ag, df_top_ag
    finally:
        c.close()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_auditoria_cache():
    c = obtener_conexion()
    try:
        asegurar_esquema(c)
        df_resumen = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha_operativa,
                   COUNT(*) AS paises_cargados,
                   GROUP_CONCAT(pais, ', ') AS lista_paises,
                   MAX(fecha_carga) AS ultima_actualizacion
            FROM dim_archivos_cargados
            GROUP BY fecha_operacion
            ORDER BY fecha_operacion DESC
            """,
            c,
        ).rename(columns={
            "fecha_operativa": "Fecha_Operativa",
            "paises_cargados": "Paises_Cargados",
            "lista_paises": "Lista_Paises",
            "ultima_actualizacion": "Ultima_Actualizacion",
        })

        df_detalle = ejecutar_consulta_df(
            """
            SELECT fecha_operacion AS fecha, codigo_osom AS oso, pais AS pais,
                   archivo_origen AS archivo_vigente, fecha_publicacion_eor AS publicacion_eor,
                   fecha_carga AS fecha_ingesta
            FROM dim_archivos_cargados
            ORDER BY fecha_operacion DESC, codigo_osom
            """,
            c,
        ).rename(columns={
            "fecha": "Fecha",
            "oso": "OSO",
            "pais": "Pais",
            "archivo_vigente": "Archivo_Vigente",
            "publicacion_eor": "Publicacion_EOR",
            "fecha_ingesta": "Fecha_Ingesta",
        })
        return df_resumen, df_detalle
    except Exception:
        return pd.DataFrame(), pd.DataFrame()
    finally:
        c.close()


def renderizar_modulo_declaraciones_contratos(mapa_agentes_dict):
    """Renderiza el módulo de recepción de declaraciones de contratos (vista acotada para agente y completa para admin)."""
    if es_admin:
        st.subheader("🚦 Declaración de Contratos Regionales (Agentes → EOR)")
        st.caption(
            "Recepción diaria de contratos para integrar en la plantilla y archivo XML del EOR. "
            "Regla de horario (Hora Honduras): declaraciones para **mañana** habilitadas hasta las **09:00 AM**; "
            "declaraciones para **después de mañana** habilitadas sin restricción."
        )
    else:
        st.subheader("📤 Carga de Declaración de Contratos Regionales")
        st.caption(
            "Sube tu archivo de declaración de contratos (`.xlsm`, `.xlsx` o `.xml`). "
            "Horario de recepción (Hora Honduras): para **mañana** hasta las **09:00 AM**; "
            "para fechas **posteriores a mañana** sin restricción."
        )

    def _fmt_ag_decl(cod):
        nom = limpiar_texto_catalogo(mapa_agentes_dict.get(limpiar_codigo(cod), ""))
        return f"{cod} - {nom}" if nom else cod

    fecha_defecto = datetime.now(ZONA_HN).date() + timedelta(days=1)
    lista_agentes_hn = sorted([c for c in DIRECTORIO_AGENTES_MER.keys() if str(c).startswith("3")])
    lista_todos_ag = sorted(list(DIRECTORIO_AGENTES_MER.keys()))
    opciones_ag_decl = lista_agentes_hn if lista_agentes_hn else lista_todos_ag

    col_f, col_ag, col_plantilla = st.columns([1.0, 1.8, 1.2])
    with col_f:
        fecha_decl_sel = st.date_input(
            "Fecha Operativa a Declarar:",
            value=fecha_defecto,
            key="decl_fecha_op",
        )
    with col_ag:
        if es_admin:
            idx_def = opciones_ag_decl.index("3DENEE") if "3DENEE" in opciones_ag_decl else 0
            agente_decl_sel = st.selectbox(
                "Agente Nacional Declarante:",
                options=opciones_ag_decl,
                index=idx_def,
                format_func=_fmt_ag_decl,
                key="decl_agente_sel",
            )
        else:
            ag_asignado = limpiar_codigo(usuario_actual.get("agente", "")).upper()
            if not ag_asignado or not ag_asignado.startswith("3"):
                st.error(
                    "Tu usuario no tiene un código de agente nacional válido asignado (debe iniciar con '3'). "
                    "Contacta al administrador."
                )
                return
            agente_decl_sel = ag_asignado
            st.text_input(
                "Agente Nacional Autorizado:",
                value=_fmt_ag_decl(agente_decl_sel),
                disabled=True,
                key="decl_agente_fijo",
            )

    fecha_decl_iso = fecha_decl_sel.strftime("%Y-%m-%d")

    with col_plantilla:
        st.markdown("<br>", unsafe_allow_html=True)
        plantilla_vacia_bytes = generar_excel_consolidado_eor(
            df_detalle=pd.DataFrame(),
            fecha_operacion=fecha_decl_iso,
            ruta_plantilla="HON20261129.xlsm",
        )
        st.download_button(
            label="📥 Plantilla Base Excel (.xlsx)",
            data=plantilla_vacia_bytes,
            file_name=f"Plantilla_Contratos_EOR_{fecha_decl_iso.replace('-', '')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="dl_plantilla_vacia_decl",
        )

    semaforo = evaluar_semaforo_declaracion(fecha_decl_iso)
    if semaforo["color"] == "VERDE":
        st.success(f"**{semaforo['estado_texto']}** — {semaforo['detalle']}")
    else:
        st.error(f"**{semaforo['estado_texto']}** — {semaforo['detalle']}")

    permitir_envio = bool(semaforo["habilitado"])
    if not permitir_envio and es_admin:
        permitir_envio = st.checkbox(
            "🔓 Habilitar carga administrativa excepcional (fuera de horario/ventana)",
            value=False,
            key="decl_override_admin",
        )

    if "uploader_decl_key" not in st.session_state:
        st.session_state["uploader_decl_key"] = 0

    archivo_decl = st.file_uploader(
        "Subir archivo de declaración de contratos (.xlsm, .xlsx o .xml):",
        type=["xlsm", "xlsx", "xml"],
        disabled=not permitir_envio,
        key=f"decl_uploader_{st.session_state['uploader_decl_key']}",
    )

    if archivo_decl is not None:
        c_val = obtener_conexion()
        try:
            asegurar_esquema(c_val)
            meta_p, regs_p, hall_p = parsear_archivo_declaracion(archivo_decl.getvalue(), archivo_decl.name)
            regs_limpios, df_hallazgos, bloqueado = validar_declaracion_contratos(
                meta=meta_p,
                registros=regs_p,
                fecha_operacion_esperada=fecha_decl_iso,
                codigo_agente_autorizado=agente_decl_sel,
                es_admin=bool(es_admin),
                conn=c_val,
                hallazgos_previos=hall_p,
            )
        finally:
            c_val.close()

        n_err = int((df_hallazgos["severidad"] == "ERROR").sum()) if not df_hallazgos.empty else 0
        n_adv = int((df_hallazgos["severidad"] == "ADVERTENCIA").sum()) if not df_hallazgos.empty else 0

        m1, m2, m3 = st.columns(3)
        m1.metric("Filas de contrato detectadas", len(regs_limpios))
        m2.metric("Errores bloqueantes", n_err)
        m3.metric("Advertencias", n_adv)

        if not df_hallazgos.empty:
            st.markdown("**Resultado de Validación del Archivo:**")
            st.dataframe(df_hallazgos, use_container_width=True, hide_index=True)

        if regs_limpios:
            with st.expander("👁️ Vista previa de tus registros detectados", expanded=False):
                st.dataframe(pd.DataFrame(regs_limpios), use_container_width=True, hide_index=True)

        if bloqueado:
            st.error("⛔ El archivo contiene **ERRORES bloqueantes** (o filas/contratos de otro agente). Corrígelos y vuelve a subirlo.")
        else:
            if n_adv == 0:
                st.success("✅ Validación exitosa sin errores ni advertencias.")
            confirmar_adv = True
            if n_adv > 0:
                confirmar_adv = st.checkbox(
                    "Confirmo que revisé las advertencias y deseo registrar esta declaración.",
                    value=False,
                    key="decl_conf_adv",
                )

            if st.button(
                "💾 Enviar y Registrar Declaración",
                type="primary",
                disabled=not (permitir_envio and confirmar_adv),
                key="btn_guardar_decl",
            ):
                c_save = obtener_conexion()
                try:
                    asegurar_esquema(c_save)
                    id_nv = guardar_declaracion_agente(
                        conn=c_save,
                        fecha_operacion=fecha_decl_iso,
                        codigo_agente=agente_decl_sel,
                        usuario_envio=usuario_actual.get("usuario", "usuario"),
                        rol_envio=usuario_actual.get("rol", "agente"),
                        nombre_archivo=archivo_decl.name,
                        meta=meta_p,
                        registros_limpios=regs_limpios,
                        total_errores=n_err,
                        total_advertencias=n_adv,
                    )
                finally:
                    c_save.close()

                st.session_state["uploader_decl_key"] += 1
                st.success(
                    f"✅ Declaración **#{id_nv}** registrada como **VIGENTE** para `{agente_decl_sel}` "
                    f"(Fecha operativa: `{fecha_decl_iso}`)."
                )
                st.rerun()

    st.divider()
    st.markdown(
        f"### 📋 {'Historial de Declaraciones Recibidas' if es_admin else 'Mis Envíos Registrados'} ({fecha_decl_iso})"
    )
    c_hist = obtener_conexion()
    try:
        asegurar_esquema(c_hist)
        df_envios = obtener_envios_fecha(c_hist, fecha_decl_iso, None if es_admin else agente_decl_sel)
        df_cons = obtener_detalle_consolidado_fecha(c_hist, fecha_decl_iso) if es_admin else pd.DataFrame()
    finally:
        c_hist.close()

    if not df_envios.empty:
        if not es_admin:
            cols_agente_vista = [
                c for c in [
                    "id_envio",
                    "fecha_operacion",
                    "codigo_agente",
                    "nombre_archivo",
                    "estado_vigencia",
                    "total_filas",
                    "total_advertencias",
                    "enviado_en",
                ]
                if c in df_envios.columns
            ]
            st.dataframe(df_envios[cols_agente_vista], use_container_width=True, hide_index=True)
        else:
            st.dataframe(df_envios, use_container_width=True, hide_index=True)
    else:
        st.info(f"Aún no hay declaraciones registradas para `{fecha_decl_iso}`.")

    if es_admin:
        st.divider()
        st.markdown(f"### 🧩 Integración Consolidada para EOR (`{fecha_decl_iso}`)")
        st.caption(
            "Incluye únicamente la **última declaración vigente** de cada agente para esta fecha operativa."
        )

        if df_cons.empty:
            st.info("No hay declaraciones vigentes para consolidar en esta fecha.")
        else:
            agentes_incluidos = sorted(df_cons["codigo_agente"].unique().tolist())
            st.success(
                f"**{len(agentes_incluidos)} agente(s)** integrados ({', '.join(agentes_incluidos)}) · "
                f"**{len(df_cons)} fila(s)** vigentes listas para el EOR."
            )
            st.dataframe(df_cons, use_container_width=True, hide_index=True)

            col_ex1, col_ex2 = st.columns(2)
            with col_ex1:
                xlsx_cons_bytes = generar_excel_consolidado_eor(
                    df_detalle=df_cons,
                    fecha_operacion=fecha_decl_iso,
                    ruta_plantilla="HON20261129.xlsm",
                )
                st.download_button(
                    label="📊 Descargar Plantilla Excel Integrada (.xlsx)",
                    data=xlsx_cons_bytes,
                    file_name=f"HON{fecha_decl_iso.replace('-', '')}_integrada.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    use_container_width=True,
                    key="dl_decl_xlsx_eor",
                )
            with col_ex2:
                xml_cons_bytes = generar_xml_consolidado_eor(
                    df_detalle=df_cons,
                    fecha_operacion=fecha_decl_iso,
                    operador="ENEE-HN",
                )
                st.download_button(
                    label=f"📄 Descargar XML EOR (HON{fecha_decl_iso.replace('-', '')}con.xml)",
                    data=xml_cons_bytes,
                    file_name=f"HON{fecha_decl_iso.replace('-', '')}con.xml",
                    mime="application/xml",
                    type="primary",
                    use_container_width=True,
                    key="dl_decl_xml_eor",
                )


# Inicializar entorno una única vez por proceso
usa_pg = preparar_entorno_bd()

if "uploader_zip_key" not in st.session_state:
    st.session_state["uploader_zip_key"] = 0

# ==========================================
# ENCABEZADO Y BARRA LATERAL
# ==========================================
if es_agente:
    st.title("⚡ Portal de Declaración de Contratos MER")
    st.caption("Carga y validación de contratos regionales para integración con el Operador de Mercado")
else:
    st.title("⚡ Sistema Histórico del Mercado Eléctrico Regional (MER)")
    st.caption("Ingesta de Predespachos (PUB004), Declaración de Contratos EOR, Catálogo y Reportería Horaria / CND")

if es_admin:
    rol_badge = "Administrador"
elif es_agente:
    rol_badge = f"Agente ({usuario_actual.get('agente') or 'Autorizado'})"
else:
    rol_badge = "Consulta"

st.sidebar.markdown(f"### 👋 Hola, **{usuario_actual['nombre']}**")
st.sidebar.caption(f"Usuario: `{usuario_actual['usuario']}` · Rol: **{rol_badge}**")

if st.sidebar.button("🚪 Cerrar sesión", use_container_width=True):
    st.session_state["usuario_activo"] = None
    st.rerun()

mapa_agentes_global = cargar_diccionario_agentes_cache()

# ==========================================
# MODO RESTRINGIDO EXCLUSIVO PARA AGENTE
# ==========================================
if es_agente:
    renderizar_modulo_declaraciones_contratos(mapa_agentes_global)
    st.stop()

# ==========================================
# BARRA LATERAL Y PESTAÑAS PARA ADMIN / CONSULTA
# ==========================================
st.sidebar.divider()

if usa_pg:
    st.sidebar.success(f"☁️ Conectado a Supabase (Esquema `{ESQUEMA_PG}`)")
else:
    st.sidebar.info("💻 Conectado a SQLite Local")

st.sidebar.header("🔍 Filtros de Consulta")

tiene_datos, min_f_str, max_f_str, fechas_disp = cargar_metadatos_fechas()

if tiene_datos:
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
        min_date = datetime.strptime(min_f_str, "%Y-%m-%d").date()
        max_date = datetime.strptime(max_f_str, "%Y-%m-%d").date()
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
    st.sidebar.info("Aún no hay datos cargados.")
    f_ini, f_fin, pais_cod = None, None, None

if es_admin:
    tab_reportes, tab_declaraciones, tab_catalogo, tab_ingesta, tab_auditoria = st.tabs([
        "📊 Reportería y Balance MER",
        "🚦 Declaración Contratos EOR",
        "🏷️ Catálogo de Nodos y Agentes",
        "📥 Ingreso de Archivos (ZIP)",
        "🗂️ Auditoría de Base de Datos",
    ])
else:
    tab_reportes, tab_catalogo, tab_auditoria = st.tabs([
        "📊 Reportería y Balance MER",
        "🏷️ Catálogo de Nodos y Agentes",
        "🗂️ Auditoría de Base de Datos",
    ])
    tab_declaraciones = None
    tab_ingesta = None

# ------------------------------------------
# PESTAÑA: DECLARACIÓN DE CONTRATOS EOR
# ------------------------------------------
if tab_declaraciones is not None:
    with tab_declaraciones:
        renderizar_modulo_declaraciones_contratos(mapa_agentes_global)

# ------------------------------------------
# PESTAÑA 1: REPORTERÍA Y VISUALIZACIÓN
# ------------------------------------------
with tab_reportes:
    if not tiene_datos:
        st.warning("No hay registros disponibles para mostrar.")
    else:
        df_pex, df_top_res, df_tcp_res, err_ind = cargar_datos_indicadores_y_precios(f_ini, f_fin, pais_cod)
        if err_ind:
            st.warning(f"Aviso al consultar indicadores ({f_ini}): {err_ind}")

        def extraer_val(df, tipo, col):
            if df.empty or "tipo" not in df.columns:
                return 0.0
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

        mapa_agentes = mapa_agentes_global

        def fmt_ag(cod):
            nom = limpiar_texto_catalogo(mapa_agentes.get(limpiar_codigo(cod), ""))
            return f"{cod} - {nom}" if nom else cod

        # ==========================================
        # SECCIÓN: INFORME DIARIO DE TRANSACCIONES REGIONALES POR AGENTE (PDF CND)
        # ==========================================
        st.subheader("📑 Informe Diario de Transacciones Regionales por Agente (PDF CND)")
        st.caption(
            "Genera el cuadro diario horario (00:00 a 23:00) por agente nacional desglosando cada contrato por contraparte y punto de medida, más compras y ventas de oportunidad."
        )

        agentes_activos_diario = cargar_agentes_nacionales_activos()
        if not agentes_activos_diario:
            st.info("Aún no hay agentes nacionales registrados para generar el informe diario.")
        else:
            col_d1, col_d2, col_d3, col_d4 = st.columns([1.0, 1.7, 1.1, 1.2])
            with col_d1:
                fecha_diario_sel = st.selectbox(
                    "Fecha Operativa:",
                    options=fechas_disp,
                    index=0,
                    key="diario_ag_fecha",
                    on_change=limpiar_descarga,
                    args=("pdf_diario_ag_bytes", "pdf_diario_ag_nombre"),
                )
            with col_d2:
                idx_denee = agentes_activos_diario.index("3DENEE") if "3DENEE" in agentes_activos_diario else 0
                ag_diario_sel = st.selectbox(
                    "Agente Nacional:",
                    options=agentes_activos_diario,
                    index=idx_denee,
                    format_func=fmt_ag,
                    key="diario_ag_cod",
                    on_change=limpiar_descarga,
                    args=("pdf_diario_ag_bytes", "pdf_diario_ag_nombre"),
                )
            with col_d3:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("📄 Generar PDF Diario Agente", type="primary", use_container_width=True, key="btn_pdf_diario_ag"):
                    c_d_pdf = obtener_conexion()
                    try:
                        asegurar_esquema(c_d_pdf)
                        ruta_pdf_d = generar_pdf_diario_agente_cnd(
                            conn=c_d_pdf,
                            fecha_operacion=fecha_diario_sel,
                            codigo_agente=ag_diario_sel,
                            ruta_logo="logo_cnd.png",
                            elaborado_por=usuario_actual.get("nombre", ""),
                        )
                    finally:
                        c_d_pdf.close()

                    if os.path.exists(ruta_pdf_d):
                        with open(ruta_pdf_d, "rb") as f_d:
                            st.session_state["pdf_diario_ag_bytes"] = f_d.read()
                            st.session_state["pdf_diario_ag_nombre"] = os.path.basename(ruta_pdf_d)

            with col_d4:
                st.markdown("<br>", unsafe_allow_html=True)
                if "pdf_diario_ag_bytes" in st.session_state:
                    st.download_button(
                        label=f"⬇️ Descargar {st.session_state['pdf_diario_ag_nombre']}",
                        data=st.session_state["pdf_diario_ag_bytes"],
                        file_name=st.session_state["pdf_diario_ag_nombre"],
                        mime="application/pdf",
                        use_container_width=True,
                        key="dl_pdf_diario_ag",
                        on_click=limpiar_descarga,
                        args=("pdf_diario_ag_bytes", "pdf_diario_ag_nombre"),
                    )

            datos_diario_prev = cargar_datos_diarios_agente_cache(fecha_diario_sel, ag_diario_sel)
            df_d_prev = datos_diario_prev["df_resumen"].copy()
            if not df_d_prev.empty:
                fila_tot_d = {"Hora": "TOTAL"}
                for col_d in df_d_prev.columns:
                    if col_d == "Hora":
                        continue
                    if "(US$/MWh)" in col_d:
                        col_mwh_ref = col_d.replace("(US$/MWh)", "(MWh)")
                        tot_m = df_d_prev[col_mwh_ref].sum() if col_mwh_ref in df_d_prev.columns else 0.0
                        fila_tot_d[col_d] = (
                            float((df_d_prev[col_d] * df_d_prev[col_mwh_ref]).sum() / tot_m)
                            if tot_m > 1e-9
                            else 0.0
                        )
                    else:
                        fila_tot_d[col_d] = float(df_d_prev[col_d].sum())
                df_d_prev = pd.concat([df_d_prev, pd.DataFrame([fila_tot_d])], ignore_index=True)

                fmt_d_cols = {}
                for col_d in df_d_prev.columns:
                    if col_d == "Hora":
                        continue
                    if "Op." in col_d:
                        fmt_d_cols[col_d] = "{:,.2f}"
                    else:
                        fmt_d_cols[col_d] = "{:,.3f}"

                st.dataframe(
                    df_d_prev.style.format(fmt_d_cols),
                    use_container_width=True,
                    hide_index=True,
                    height=310,
                )

        st.divider()

        # ==========================================
        # SECCIÓN: REPORTE MENSUAL OFICIAL CND (PDF CON LOGO)
        # ==========================================
        st.subheader("📄 Reporte Mensual Oficial CND (Impresión PDF con Logo)")
        st.caption("Genera el cuadro diario mensual de energía suministrada por contrato (CF / CNFFF) entre un Agente Nacional y su Contraparte, listo para imprimir en PDF.")

        pares_tcp = cargar_pares_tcp()

        if pares_tcp.empty:
            st.info("Aún no hay registros con Agente Contraparte en `fact_contratos_tcp` para generar el reporte mensual CND.")
        else:
            meses_disponibles = sorted(list({str(f)[:7] for f in fechas_disp}), reverse=True)

            col_cnd1, col_cnd2, col_cnd3, col_cnd4 = st.columns([1.1, 1.4, 1.4, 1.1])
            with col_cnd1:
                mes_sel = st.selectbox("Mes del Reporte (AAAA-MM):", options=meses_disponibles, key="cnd_mes")
                anio_cnd = int(mes_sel.split("-")[0])
                mes_num_cnd = int(mes_sel.split("-")[1])

            with col_cnd2:
                lista_ag_nac = sorted(pares_tcp["agente_nac"].unique().tolist())
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

            col_nom1, col_nom2, col_nom3 = st.columns(3)
            cp_clean = limpiar_codigo(cp_sel)
            ag_nac_clean = limpiar_codigo(ag_nac_sel)

            nom_default_sumin = limpiar_texto_catalogo(mapa_agentes.get(cp_clean)) or cp_clean
            nom_default_recep = (
                "ENEE"
                if "ENEE" in ag_nac_clean
                else (limpiar_texto_catalogo(mapa_agentes.get(ag_nac_clean)) or ag_nac_clean)
            )
            nom_default_elab = usuario_actual.get("nombre", "")

            with col_nom1:
                nombre_sumin_pdf = st.text_input(
                    "Nombre del Suministrador en el Título PDF:",
                    value=nom_default_sumin,
                    key=f"txt_sumin_{cp_clean}",
                )
            with col_nom2:
                nombre_recep_pdf = st.text_input(
                    "Nombre del Receptor en el Título PDF:",
                    value=nom_default_recep,
                    key=f"txt_recep_{ag_nac_clean}",
                )
            with col_nom3:
                elaborado_por_pdf = st.text_input(
                    "Elaborado por:",
                    value=nom_default_elab,
                    key="txt_elaborado_por_cnd",
                )

            df_preview_cnd = cargar_df_mensual_cnd_cache(
                anio_cnd, mes_num_cnd, ag_nac_sel, cp_sel, tipo_tr_cnd
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
                    c_pdf = obtener_conexion()
                    try:
                        asegurar_esquema(c_pdf)
                        ruta_pdf = generar_pdf_reporte_cnd(
                            conn=c_pdf,
                            anio=anio_cnd,
                            mes=mes_num_cnd,
                            agente_nacional=ag_nac_sel,
                            agente_contraparte=cp_sel,
                            nombre_suministrador=nombre_sumin_pdf,
                            nombre_receptor=nombre_recep_pdf,
                            tipo_transaccion=tipo_tr_cnd,
                            ruta_logo="logo_cnd.png",
                            elaborado_por=elaborado_por_pdf,
                        )
                    finally:
                        c_pdf.close()

                    if os.path.exists(ruta_pdf):
                        with open(ruta_pdf, "rb") as f_pdf:
                            st.session_state["pdf_cnd_bytes"] = f_pdf.read()
                            st.session_state["pdf_cnd_nombre"] = os.path.basename(ruta_pdf)

                if "pdf_cnd_bytes" in st.session_state:
                    st.download_button(
                        label=f"⬇️ Descargar {st.session_state['pdf_cnd_nombre']}",
                        data=st.session_state["pdf_cnd_bytes"],
                        file_name=st.session_state["pdf_cnd_nombre"],
                        mime="application/pdf",
                        use_container_width=True,
                        on_click=limpiar_descarga,
                        args=("pdf_cnd_bytes", "pdf_cnd_nombre"),
                    )

        st.divider()

        # ==========================================
        # REPORTE ANUAL CND: TRANSACCIONES MER (TABLA + GRÁFICAS PDF)
        # ==========================================
        st.subheader("📊 Reporte Anual CND: Transacciones de Energía en el MER (PDF con Gráficas)")
        st.caption(
            f"Consolida de enero hasta el mes seleccionado las compras y ventas (Contratos y Oportunidad) para el área seleccionada ({pais_cod or 'REGIONAL MER'})."
        )

        anios_disponibles = sorted(list({int(str(f)[:4]) for f in fechas_disp}), reverse=True)
        opciones_meses_corte = [
            (1, "01 - Enero"),
            (2, "02 - Febrero"),
            (3, "03 - Marzo"),
            (4, "04 - Abril"),
            (5, "05 - Mayo"),
            (6, "06 - Junio"),
            (7, "07 - Julio"),
            (8, "08 - Agosto"),
            (9, "09 - Septiembre"),
            (10, "10 - Octubre"),
            (11, "11 - Noviembre"),
            (12, "12 - Diciembre"),
        ]

        col_an_sel, col_mes_corte, col_an_btn, col_an_dl = st.columns([0.9, 1.2, 1.2, 1.5])

        with col_an_sel:
            anio_sel_cnd = st.selectbox(
                "Año del Reporte:",
                options=anios_disponibles,
                key="sel_anio_cnd_anual",
                on_change=limpiar_descarga,
                args=("pdf_anual_bytes", "pdf_anual_nombre"),
            )

        with col_mes_corte:
            hoy = datetime.now()
            if anio_sel_cnd == hoy.year and hoy.month > 1:
                idx_mes_default = hoy.month - 2
            else:
                idx_mes_default = 11

            mes_corte_opcion = st.selectbox(
                "Cubrir hasta el mes de:",
                options=opciones_meses_corte,
                index=idx_mes_default,
                format_func=lambda x: x[1],
                key="sel_mes_corte_anual",
                on_change=limpiar_descarga,
                args=("pdf_anual_bytes", "pdf_anual_nombre"),
            )
            mes_hasta_cnd = mes_corte_opcion[0]

        with col_an_btn:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("📄 Generar PDF Anual MER", type="primary", use_container_width=True, key="btn_pdf_anual_mer"):
                c_an = obtener_conexion()
                try:
                    asegurar_esquema(c_an)
                    ruta_pdf_an = generar_pdf_transacciones_anuales_mer(
                        conn=c_an,
                        anio=anio_sel_cnd,
                        pais_filtro=pais_cod,
                        mes_hasta=mes_hasta_cnd,
                        ruta_logo="logo_cnd.png",
                    )
                finally:
                    c_an.close()

                if os.path.exists(ruta_pdf_an):
                    with open(ruta_pdf_an, "rb") as f_an:
                        st.session_state["pdf_anual_bytes"] = f_an.read()
                        st.session_state["pdf_anual_nombre"] = os.path.basename(ruta_pdf_an)

        with col_an_dl:
            st.markdown("<br>", unsafe_allow_html=True)
            if "pdf_anual_bytes" in st.session_state:
                st.download_button(
                    label=f"⬇️ Descargar {st.session_state['pdf_anual_nombre']}",
                    data=st.session_state["pdf_anual_bytes"],
                    file_name=st.session_state["pdf_anual_nombre"],
                    mime="application/pdf",
                    use_container_width=True,
                    key="dl_pdf_anual_mer",
                    on_click=limpiar_descarga,
                    args=("pdf_anual_bytes", "pdf_anual_nombre"),
                )

        df_anual_prev = cargar_df_anual_mer_cache(anio_sel_cnd, pais_cod, mes_hasta_cnd)
        if not df_anual_prev.empty:
            df_anual_show = df_anual_prev[[
                "Fecha",
                "Compras_Contratos",
                "Compras_Oportunidad",
                "Ventas_Contratos",
                "Ventas_Oportunidad",
                "Total_Compras",
                "Total_Ventas",
            ]].rename(columns={
                "Compras_Contratos": "COMPRAS CONTRATOS (MWh)",
                "Compras_Oportunidad": "COMPRAS OPORTUNIDAD (MWh)",
                "Ventas_Contratos": "VENTAS CONTRATOS (MWh)",
                "Ventas_Oportunidad": "VENTAS OPORTUNIDAD (MWh)",
                "Total_Compras": "TOTAL COMPRAS (MWh)",
                "Total_Ventas": "TOTAL VENTAS (MWh)",
            })
            fila_tot_an = pd.DataFrame([{
                "Fecha": "Total",
                "COMPRAS CONTRATOS (MWh)": df_anual_show["COMPRAS CONTRATOS (MWh)"].dropna().sum(),
                "COMPRAS OPORTUNIDAD (MWh)": df_anual_show["COMPRAS OPORTUNIDAD (MWh)"].dropna().sum(),
                "VENTAS CONTRATOS (MWh)": df_anual_show["VENTAS CONTRATOS (MWh)"].dropna().sum(),
                "VENTAS OPORTUNIDAD (MWh)": df_anual_show["VENTAS OPORTUNIDAD (MWh)"].dropna().sum(),
                "TOTAL COMPRAS (MWh)": df_anual_show["TOTAL COMPRAS (MWh)"].dropna().sum(),
                "TOTAL VENTAS (MWh)": df_anual_show["TOTAL VENTAS (MWh)"].dropna().sum(),
            }])
            df_anual_show = pd.concat([df_anual_show, fila_tot_an], ignore_index=True)

            def _fmt_an_web(v):
                if v is None or pd.isna(v):
                    return ""
                if abs(float(v)) < 0.0005:
                    return "-"
                return f"{float(v):,.3f}"

            cols_an_num = [c for c in df_anual_show.columns if c != "Fecha"]
            st.dataframe(
                df_anual_show.style.format({c: _fmt_an_web for c in cols_an_num}),
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        # ==========================================
        # TABLA 1: BALANCE HORARIO DE RETIROS E INYECCIONES (3 DECIMALES)
        # ==========================================
        st.subheader(f"📋 Balance Horario de Transacciones ({pais_cod or 'REGIONAL MER'})")
        df_bal_web, df_tcp_ag, df_top_ag = cargar_tablas_horarias_cache(f_ini, f_fin, pais_cod)

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
                c_cv = obtener_conexion()
                try:
                    asegurar_esquema(c_cv)
                    ruta_pdf_cv = generar_pdf_informe_compra_venta(
                        conn=c_cv,
                        fecha_inicio=f_ini,
                        fecha_fin=f_fin,
                        pais_filtro=pais_cod,
                        ruta_logo="logo_cnd.png",
                    )
                finally:
                    c_cv.close()

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
                    on_click=limpiar_descarga,
                    args=("pdf_cv_bytes", "pdf_cv_nombre"),
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
            if not df_tcp_ag.empty:
                for col_nom in ("Nombre_Agente", "Nombre_Contraparte"):
                    if col_nom in df_tcp_ag.columns:
                        df_tcp_ag[col_nom] = df_tcp_ag[col_nom].apply(limpiar_texto_catalogo)

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
                        "Tipo_Oferta": "",
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
            if not df_top_ag.empty:
                if "Nombre_Agente" in df_top_ag.columns:
                    df_top_ag["Nombre_Agente"] = df_top_ag["Nombre_Agente"].apply(limpiar_texto_catalogo)

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
                            st.session_state["xlsx_bytes"] = f_excel.read()
                            st.session_state["xlsx_nombre"] = archivo_xlsx

        with col_btn2:
            if "xlsx_bytes" in st.session_state:
                st.download_button(
                    label=f"⬇️ Descargar {st.session_state['xlsx_nombre']}",
                    data=st.session_state["xlsx_bytes"],
                    file_name=st.session_state["xlsx_nombre"],
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    on_click=limpiar_descarga,
                    args=("xlsx_bytes", "xlsx_nombre"),
                )

# ------------------------------------------
# PESTAÑA 2: CATÁLOGO DE NODOS Y AGENTES
# ------------------------------------------
with tab_catalogo:
    st.subheader("🏷️ Catálogo de Nombres Reales (Nodos y Agentes)")
    st.markdown("Los códigos se detectan automáticamente de los archivos `PUB004` y se vinculan con el directorio oficial del MER.")

    if not es_admin:
        st.info(f"🔒 Sesión de **{usuario_actual['nombre']}** (Solo Consulta). Únicamente usuarios administradores pueden editar nombres.")

    subtab_nodos, subtab_agentes, subtab_nac, subtab_cf, subtab_excel_cat = st.tabs([
        "🔌 Catálogo de Nodos Eléctricos",
        "🏢 Catálogo de Agentes MER",
        "🇭🇳 Agentes Nacionales",
        "📜 Contratos Firmes",
        "📤 Importar / Exportar Catálogo Excel",
    ])

    with subtab_nodos:
        filtro_pais_cat = st.selectbox(
            "Filtrar nodos por país:",
            options=["TODOS", "HN", "GT", "SV", "NI", "CR", "PA"],
            index=1 if pais_cod == "HN" else 0,
            key="cat_pais_nodos",
        )
        c_cat_n = obtener_conexion()
        try:
            asegurar_esquema(c_cat_n)
            q_nodos = "SELECT codigo_nodo, pais, nombre_nodo, subestacion, nivel_kv FROM dim_catalogo_nodos"
            p_nodos = []
            if filtro_pais_cat != "TODOS":
                q_nodos += " WHERE pais = ?"
                p_nodos.append(filtro_pais_cat)
            q_nodos += " ORDER BY codigo_nodo"
            df_nodos_cat = ejecutar_consulta_df(q_nodos, c_cat_n, params=p_nodos)
        finally:
            c_cat_n.close()

        for col_n in ("nombre_nodo", "subestacion", "nivel_kv"):
            if col_n in df_nodos_cat.columns:
                df_nodos_cat[col_n] = df_nodos_cat[col_n].apply(limpiar_texto_catalogo)

        if es_admin:
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
                c_save_n = obtener_conexion()
                asegurar_esquema(c_save_n)
                cur = c_save_n.cursor()
                for _, row in edited_nodos.iterrows():
                    ejecutar_sql(
                        cur,
                        """
                        UPDATE dim_catalogo_nodos
                        SET nombre_nodo = ?, subestacion = ?, nivel_kv = ?
                        WHERE codigo_nodo = ?
                        """,
                        (
                            limpiar_texto_catalogo(row["nombre_nodo"]),
                            limpiar_texto_catalogo(row["subestacion"]),
                            limpiar_texto_catalogo(row["nivel_kv"]),
                            row["codigo_nodo"],
                        ),
                    )
                c_save_n.commit()
                cur.close()
                c_save_n.close()
                st.cache_data.clear()
                st.success("✅ Catálogo de nodos actualizado correctamente.")
                st.rerun()
        else:
            st.dataframe(df_nodos_cat, use_container_width=True, hide_index=True)

    with subtab_agentes:
        c_cat_a = obtener_conexion()
        try:
            asegurar_esquema(c_cat_a)
            df_agentes_cat = ejecutar_consulta_df(
                "SELECT codigo_agente, pais, nombre_agente, tipo_agente FROM dim_catalogo_agentes ORDER BY pais, codigo_agente",
                c_cat_a,
            )
        finally:
            c_cat_a.close()

        for col_a in ("nombre_agente", "tipo_agente"):
            if col_a in df_agentes_cat.columns:
                df_agentes_cat[col_a] = df_agentes_cat[col_a].apply(limpiar_texto_catalogo)

        if es_admin:
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
                c_save_a = obtener_conexion()
                asegurar_esquema(c_save_a)
                cur = c_save_a.cursor()
                for _, row in edited_agentes.iterrows():
                    ejecutar_sql(
                        cur,
                        """
                        UPDATE dim_catalogo_agentes
                        SET nombre_agente = ?, tipo_agente = ?
                        WHERE codigo_agente = ?
                        """,
                        (
                            limpiar_texto_catalogo(row["nombre_agente"]),
                            limpiar_texto_catalogo(row["tipo_agente"]),
                            row["codigo_agente"],
                        ),
                    )
                c_save_a.commit()
                cur.close()
                c_save_a.close()
                st.cache_data.clear()
                st.success("✅ Catálogo de agentes actualizado correctamente.")
                st.rerun()
        else:
            st.dataframe(df_agentes_cat, use_container_width=True, hide_index=True)

    with subtab_nac:
        st.markdown("Agentes nacionales sincronizados automáticamente desde el catálogo del MER (código iniciado con **3**).")
        c_cat_nac = obtener_conexion()
        try:
            asegurar_esquema(c_cat_nac)
            df_nac_cat = ejecutar_consulta_df(
                "SELECT codigo_agente, nombre_agente, COALESCE(activo, 1) AS activo FROM dim_agentes_nacionales ORDER BY codigo_agente",
                c_cat_nac,
            )
            if df_nac_cat.empty:
                inicializar_y_sincronizar_catalogo(c_cat_nac)
                df_nac_cat = ejecutar_consulta_df(
                    "SELECT codigo_agente, nombre_agente, COALESCE(activo, 1) AS activo FROM dim_agentes_nacionales ORDER BY codigo_agente",
                    c_cat_nac,
                )
        finally:
            c_cat_nac.close()

        if df_nac_cat.empty:
            df_nac_cat = pd.DataFrame(columns=["codigo_agente", "nombre_agente", "activo"])
        else:
            df_nac_cat["codigo_agente"] = df_nac_cat["codigo_agente"].apply(lambda x: limpiar_codigo(x).upper())
            df_nac_cat["nombre_agente"] = df_nac_cat["nombre_agente"].apply(limpiar_texto_catalogo)
            df_nac_cat["activo"] = df_nac_cat["activo"].apply(lambda x: bool(int(x)) if pd.notna(x) else True)

        if es_admin:
            edited_nac = st.data_editor(
                df_nac_cat,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "codigo_agente": st.column_config.TextColumn("Código Agente Nacional (Ej. 3DENEE)", required=True),
                    "nombre_agente": st.column_config.TextColumn("Nombre / Razón Social", width="large"),
                    "activo": st.column_config.CheckboxColumn("Activo", default=True),
                },
                key="editor_agentes_nacionales",
            )

            if st.button("💾 Guardar Agentes Nacionales", type="primary", key="btn_save_nac"):
                c_save_nac = obtener_conexion()
                asegurar_esquema(c_save_nac)
                cur = c_save_nac.cursor()
                codigos_en_tabla = []
                invalidos = []
                for _, row in edited_nac.iterrows():
                    cod = limpiar_codigo(row.get("codigo_agente")).upper()
                    if not cod:
                        continue
                    if not cod.startswith("3"):
                        invalidos.append(cod)
                        continue
                    codigos_en_tabla.append(cod)
                    nom = limpiar_texto_catalogo(row.get("nombre_agente"))
                    act_val = 1 if bool(row.get("activo", True)) else 0
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_agentes_nacionales (codigo_agente, nombre_agente, activo)
                        VALUES (?, ?, ?)
                        ON CONFLICT (codigo_agente) DO UPDATE SET
                            nombre_agente = excluded.nombre_agente,
                            activo = excluded.activo
                        """,
                        (cod, nom, act_val),
                    )
                if codigos_en_tabla:
                    placeholders = ", ".join(["?"] * len(codigos_en_tabla))
                    ejecutar_sql(
                        cur,
                        f"DELETE FROM dim_agentes_nacionales WHERE codigo_agente NOT IN ({placeholders})",
                        tuple(codigos_en_tabla),
                    )
                else:
                    ejecutar_sql(cur, "DELETE FROM dim_agentes_nacionales")
                c_save_nac.commit()
                cur.close()
                c_save_nac.close()
                st.cache_data.clear()
                if invalidos:
                    st.warning(f"Se omitieron códigos que no inician con '3': {', '.join(invalidos)}")
                st.success("✅ Tabla de agentes nacionales actualizada.")
                st.rerun()
        else:
            st.dataframe(df_nac_cat, use_container_width=True, hide_index=True)

    with subtab_cf:
        st.markdown("Asigna cada **Código Contrato Firme** (ej. `CF13M0000704`) a su agente nacional titular para evitar declaraciones cruzadas.")
        c_cat_cf = obtener_conexion()
        try:
            asegurar_esquema(c_cat_cf)
            df_cf_cat = ejecutar_consulta_df(
                """
                SELECT codigo_contrato_firme, agente_autorizado,
                       COALESCE(agente_contraparte, '') AS agente_contraparte,
                       COALESCE(descripcion, '') AS descripcion,
                       COALESCE(activo, 1) AS activo
                  FROM dim_contratos_firmes
                 ORDER BY codigo_contrato_firme
                """,
                c_cat_cf,
            )
        finally:
            c_cat_cf.close()

        if df_cf_cat.empty:
            df_cf_cat = pd.DataFrame(columns=["codigo_contrato_firme", "agente_autorizado", "agente_contraparte", "descripcion", "activo"])
        else:
            df_cf_cat["codigo_contrato_firme"] = df_cf_cat["codigo_contrato_firme"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["agente_autorizado"] = df_cf_cat["agente_autorizado"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["agente_contraparte"] = df_cf_cat["agente_contraparte"].apply(lambda x: limpiar_codigo(x).upper())
            df_cf_cat["descripcion"] = df_cf_cat["descripcion"].apply(limpiar_texto_catalogo)
            df_cf_cat["activo"] = df_cf_cat["activo"].apply(lambda x: bool(int(x)) if pd.notna(x) else True)

        if es_admin:
            edited_cf = st.data_editor(
                df_cf_cat,
                use_container_width=True,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "codigo_contrato_firme": st.column_config.TextColumn("Código Contrato Firme", required=True),
                    "agente_autorizado": st.column_config.TextColumn("Agente Titular (Ej. 3DENEE)", required=True),
                    "agente_contraparte": st.column_config.TextColumn("Contraparte (Opcional)"),
                    "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                    "activo": st.column_config.CheckboxColumn("Activo", default=True),
                },
                key="editor_contratos_firmes",
            )

            if st.button("💾 Guardar Contratos Firmes", type="primary", key="btn_save_cf"):
                c_save_cf = obtener_conexion()
                asegurar_esquema(c_save_cf)
                cur = c_save_cf.cursor()
                cods_cf = []
                for _, row in edited_cf.iterrows():
                    cod_cf = limpiar_codigo(row.get("codigo_contrato_firme")).upper()
                    ag_tit = limpiar_codigo(row.get("agente_autorizado")).upper()
                    if not cod_cf or not ag_tit:
                        continue
                    cods_cf.append(cod_cf)
                    ag_cp = limpiar_codigo(row.get("agente_contraparte")).upper()
                    desc = limpiar_texto_catalogo(row.get("descripcion"))
                    act_val = 1 if bool(row.get("activo", True)) else 0
                    ejecutar_sql(
                        cur,
                        """
                        INSERT INTO dim_contratos_firmes (codigo_contrato_firme, agente_autorizado, agente_contraparte, descripcion, activo)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (codigo_contrato_firme) DO UPDATE SET
                            agente_autorizado = excluded.agente_autorizado,
                            agente_contraparte = excluded.agente_contraparte,
                            descripcion = excluded.descripcion,
                            activo = excluded.activo
                        """,
                        (cod_cf, ag_tit, ag_cp, desc, act_val),
                    )
                if cods_cf:
                    placeholders_cf = ", ".join(["?"] * len(cods_cf))
                    ejecutar_sql(
                        cur,
                        f"DELETE FROM dim_contratos_firmes WHERE codigo_contrato_firme NOT IN ({placeholders_cf})",
                        tuple(cods_cf),
                    )
                else:
                    ejecutar_sql(cur, "DELETE FROM dim_contratos_firmes")
                c_save_cf.commit()
                cur.close()
                c_save_cf.close()
                st.cache_data.clear()
                st.success("✅ Catálogo de contratos firmes actualizado.")
                st.rerun()
        else:
            st.dataframe(df_cf_cat, use_container_width=True, hide_index=True)

    with subtab_excel_cat:
        st.markdown("Descarga el catálogo actual a Excel para consulta o edición.")
        c_xl_cat = obtener_conexion()
        try:
            asegurar_esquema(c_xl_cat)
            buffer_cat = io.BytesIO()
            with pd.ExcelWriter(buffer_cat, engine="openpyxl") as writer:
                df_n_xl = ejecutar_consulta_df("SELECT * FROM dim_catalogo_nodos ORDER BY codigo_nodo", c_xl_cat)
                for col_n in ("nombre_nodo", "subestacion", "nivel_kv"):
                    if col_n in df_n_xl.columns:
                        df_n_xl[col_n] = df_n_xl[col_n].apply(limpiar_texto_catalogo)
                df_n_xl.to_excel(writer, sheet_name="Nodos", index=False)

                df_a_xl = ejecutar_consulta_df("SELECT * FROM dim_catalogo_agentes ORDER BY pais, codigo_agente", c_xl_cat)
                for col_a in ("nombre_agente", "tipo_agente"):
                    if col_a in df_a_xl.columns:
                        df_a_xl[col_a] = df_a_xl[col_a].apply(limpiar_texto_catalogo)
                df_a_xl.to_excel(writer, sheet_name="Agentes", index=False)
        finally:
            c_xl_cat.close()

        st.download_button(
            label="⬇️ Descargar Plantilla_Catalogo_MER.xlsx",
            data=buffer_cat.getvalue(),
            file_name="Plantilla_Catalogo_MER.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        if es_admin:
            archivo_cat_up = st.file_uploader("Subir Plantilla_Catalogo_MER.xlsx completada", type=["xlsx"])
            if archivo_cat_up and st.button("⬆️ Actualizar Catálogo desde Excel"):
                xl_cat = pd.ExcelFile(archivo_cat_up)
                c_up_cat = obtener_conexion()
                asegurar_esquema(c_up_cat)
                cur = c_up_cat.cursor()
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
                            (
                                limpiar_codigo(r["codigo_nodo"]),
                                limpiar_texto_catalogo(r["pais"]),
                                limpiar_texto_catalogo(r["nombre_nodo"]),
                                limpiar_texto_catalogo(r["subestacion"]),
                                limpiar_texto_catalogo(r["nivel_kv"]),
                            ),
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
                            (
                                limpiar_codigo(r["codigo_agente"]),
                                limpiar_texto_catalogo(r["pais"]),
                                limpiar_texto_catalogo(r["nombre_agente"]),
                                limpiar_texto_catalogo(r["tipo_agente"]),
                            ),
                        )
                c_up_cat.commit()
                cur.close()
                c_up_cat.close()
                st.cache_data.clear()
                st.success("✅ Catálogo importado exitosamente desde Excel.")
                st.rerun()

# ------------------------------------------
# PESTAÑA 3: INGRESO DE ARCHIVOS (SOLO ADMIN)
# ------------------------------------------
if es_admin and tab_ingesta is not None:
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
            key=f"uploader_zip_{st.session_state['uploader_zip_key']}",
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

                    c_sync = obtener_conexion()
                    inicializar_y_sincronizar_catalogo(c_sync)
                    c_sync.close()

                st.cache_data.clear()
                st.session_state["ultimo_log_ingesta"] = log_buffer.getvalue().strip()
                st.session_state["uploader_zip_key"] += 1
                st.rerun()

        if "ultimo_log_ingesta" in st.session_state:
            st.success("✅ ¡Sincronización completada y catálogo actualizado!")
            if st.session_state["ultimo_log_ingesta"]:
                st.code(st.session_state["ultimo_log_ingesta"], language="text")
            else:
                st.warning("No se encontraron archivos válidos con patrón PUB004-*.xlsx dentro del ZIP.")

# ------------------------------------------
# PESTAÑA 4: AUDITORÍA DEL HISTORIAL
# ------------------------------------------
if tab_auditoria is not None:
    with tab_auditoria:
        st.subheader("Estado Actual de la Base de Datos Histórica")
        if tiene_datos:
            df_resumen_dias, df_detalle = cargar_auditoria_cache()
            st.dataframe(df_resumen_dias, use_container_width=True, hide_index=True)

            st.markdown("#### Detalle de Archivos Oficiales Vigentes")
            st.dataframe(df_detalle, use_container_width=True, hide_index=True)
        else:
            st.info("No hay archivos registrados en la auditoría todavía.")
