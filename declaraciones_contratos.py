import io
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openpyxl
import pandas as pd

from db_mer import ejecutar_consulta_df, ejecutar_sql, es_postgres, ESQUEMA_PG
from catalogo_oficial_mer import DIRECTORIO_AGENTES_MER

ZONA_HN = ZoneInfo("America/Tegucigalpa")
NS_EOR = "http://www.enteoperador.org/schema/DeclaracionDeContratos"
MAX_BYTES_XML = 10 * 1024 * 1024  # 10 MB
MAX_NODOS_XML = 25000

COLUMNAS_PLANTILLA_EOR = [
    "Agente_i",
    "Punto_Med_i",
    "Agente_r",
    "Punto_Med_r",
    "Periodo_Inicio",
    "Periodo_Fin",
    "Tipo_Contrato",
    "Codigo Contrato Firme",
    "Titular DT",
    "Responsable_Transmision",
    "Energia_Declarada MW",
    "Energia_Requerida MW",
    "Oferta de PMAX",
    "MW_Bloque1",
    "Precio1",
    "MW_Bloque2",
    "Precio2",
    "MW_Bloque3",
    "Precio3",
    "MW_Bloque4",
    "Precio4",
    "MW_Bloque5",
    "Precio5",
    "Oferta de FLEX",
    "Tipo_Oferta_Flexibilidad",
    "MW_Bloque1",
    "Precio1",
    "MW_Bloque2",
    "Precio2",
]


def _limpiar_texto(val):
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except Exception:
        pass
    s = str(val).strip()
    if s.endswith(".0") and s.replace(".0", "").isdigit():
        s = s[:-2]
    if s.lower() in ("nan", "none", "null", "<na>"):
        return ""
    return s


def _a_float(val, default=0.0):
    s = _limpiar_texto(val)
    if s == "":
        return float(default), True
    s_norm = s.replace(",", "")
    try:
        f = float(s_norm)
        if pd.isna(f) or f in (float("inf"), float("-inf")):
            return None, False
        return float(f), True
    except ValueError:
        return None, False


def _a_int_periodo(val):
    s = _limpiar_texto(val)
    if s == "":
        return None, False
    try:
        f = float(s.replace(",", ""))
        if not f.is_integer():
            return None, False
        i = int(f)
        if 0 <= i <= 23:
            return i, True
        return i, False
    except ValueError:
        return None, False


def _normalizar_fecha_str(fecha_val):
    s = _limpiar_texto(fecha_val)
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _fecha_a_ddmmyyyy(fecha_iso):
    return datetime.strptime(fecha_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")


def _preparar_sesion_conn(conn):
    if conn is None or not es_postgres(conn):
        return
    try:
        conn.rollback()
    except Exception:
        pass
    cur = conn.cursor()
    try:
        cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
    finally:
        cur.close()


def evaluar_semaforo_declaracion(fecha_operacion_iso, ahora_hn=None):
    """
    Regla:
- Mañana (fecha_op == hoy + 1): VERDE hasta las 09:00 AM (<= 09:00:59), ROJO desde las 09:01 AM.
- Después de mañana (fecha_op >= hoy + 2): VERDE sin restricción horaria.
- Hoy o fechas pasadas (fecha_op <= hoy): ROJO.
    """
    if ahora_hn is None:
        ahora_hn = datetime.now(ZONA_HN)
    elif ahora_hn.tzinfo is None:
        ahora_hn = ahora_hn.replace(tzinfo=ZONA_HN)
    else:
        ahora_hn = ahora_hn.astimezone(ZONA_HN)

    hoy = ahora_hn.date()
    manana = hoy + timedelta(days=1)
    pasado_manana = hoy + timedelta(days=2)

    try:
        fecha_op = datetime.strptime(str(fecha_operacion_iso)[:10], "%Y-%m-%d").date()
    except ValueError:
        return {
            "habilitado": False,
            "color": "ROJO",
            "estado_texto": "🔴 CERRADO — Fecha inválida",
            "detalle": "Selecciona una fecha operativa válida.",
            "hora_hn": ahora_hn.strftime("%Y-%m-%d %H:%M:%S"),
        }

    corte_manana = ahora_hn.replace(hour=9, minute=0, second=59, microsecond=999999)

    if fecha_op == manana:
        if ahora_hn <= corte_manana:
            return {
                "habilitado": True,
                "color": "VERDE",
                "estado_texto": "🟢 HABILITADO (Declaración para mañana — cierre 09:00 AM)",
                "detalle": f"Hora local HN: {ahora_hn.strftime('%I:%M:%S %p')}. Tienes hasta las 09:00 AM de hoy para enviar o actualizar.",
                "hora_hn": ahora_hn.strftime("%Y-%m-%d %H:%M:%S"),
            }
        return {
            "habilitado": False,
            "color": "ROJO",
            "estado_texto": "🔴 CERRADO (Venció límite de las 09:00 AM para mañana)",
            "detalle": f"Hora local HN: {ahora_hn.strftime('%I:%M:%S %p')}. El envío para {fecha_op} cerró a las 09:00 AM.",
            "hora_hn": ahora_hn.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if fecha_op >= pasado_manana:
        return {
            "habilitado": True,
            "color": "VERDE",
            "estado_texto": "🟢 HABILITADO (Declaración anticipada — sin restricción horaria)",
            "detalle": f"Fecha operativa {fecha_op} es posterior a mañana; recepción abierta.",
            "hora_hn": ahora_hn.strftime("%Y-%m-%d %H:%M:%S"),
        }

    return {
        "habilitado": False,
        "color": "ROJO",
        "estado_texto": "🔴 CERRADO (Fecha operativa hoy o pasada)",
        "detalle": f"No se aceptan declaraciones de contratos para {fecha_op} (hoy es {hoy}).",
        "hora_hn": ahora_hn.strftime("%Y-%m-%d %H:%M:%S"),
    }


def inicializar_tablas_declaraciones(conn):
    _preparar_sesion_conn(conn)
    cur = conn.cursor()
    try:
        if es_postgres(conn):
            ejecutar_sql(
                cur,
                f"""
                CREATE TABLE IF NOT EXISTS {ESQUEMA_PG}.dim_agentes_nacionales (
                    codigo_agente TEXT PRIMARY KEY,
                    nombre_agente TEXT,
                    activo INTEGER DEFAULT 1
                );
                """,
            )
            ejecutar_sql(
                cur,
                f"""
                CREATE TABLE IF NOT EXISTS {ESQUEMA_PG}.dim_contratos_firmes (
                    codigo_contrato_firme TEXT PRIMARY KEY,
                    agente_autorizado TEXT NOT NULL,
                    agente_contraparte TEXT DEFAULT '',
                    descripcion TEXT DEFAULT '',
                    activo INTEGER DEFAULT 1
                );
                """,
            )
            ejecutar_sql(
                cur,
                f"""
                CREATE TABLE IF NOT EXISTS {ESQUEMA_PG}.fact_declaraciones_contratos_envios (
                    id_envio SERIAL PRIMARY KEY,
                    fecha_operacion TEXT NOT NULL,
                    codigo_agente TEXT NOT NULL,
                    usuario_envio TEXT NOT NULL,
                    rol_envio TEXT NOT NULL,
                    nombre_archivo TEXT,
                    operador TEXT DEFAULT 'ENEE-HN',
                    fecha_archivo TEXT,
                    estado_vigencia TEXT NOT NULL DEFAULT 'VIGENTE',
                    total_filas INTEGER DEFAULT 0,
                    total_errores INTEGER DEFAULT 0,
                    total_advertencias INTEGER DEFAULT 0,
                    enviado_en TEXT NOT NULL
                );
                """,
            )
            ejecutar_sql(
                cur,
                f"""
                CREATE TABLE IF NOT EXISTS {ESQUEMA_PG}.fact_declaraciones_contratos_detalle (
                    id_detalle SERIAL PRIMARY KEY,
                    id_envio INTEGER NOT NULL REFERENCES {ESQUEMA_PG}.fact_declaraciones_contratos_envios(id_envio) ON DELETE CASCADE,
                    fecha_operacion TEXT NOT NULL,
                    codigo_agente TEXT NOT NULL,
                    orden_fila INTEGER NOT NULL,
                    agente_i TEXT,
                    punto_med_i TEXT,
                    agente_r TEXT,
                    punto_med_r TEXT,
                    periodo_inicio INTEGER,
                    periodo_fin INTEGER,
                    tipo_contrato TEXT,
                    codigo_contrato_firme TEXT,
                    titular_dt TEXT,
                    responsable_transmision TEXT,
                    energia_declarada_mw DOUBLE PRECISION DEFAULT 0.0,
                    energia_requerida_mw DOUBLE PRECISION DEFAULT 0.0,
                    oferta_pmax_existe TEXT DEFAULT 'no',
                    pmax_mw1 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_p1 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_mw2 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_p2 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_mw3 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_p3 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_mw4 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_p4 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_mw5 DOUBLE PRECISION DEFAULT 0.0,
                    pmax_p5 DOUBLE PRECISION DEFAULT 0.0,
                    oferta_flex_existe TEXT DEFAULT 'no',
                    tipo_oferta_flexibilidad TEXT DEFAULT '',
                    flex_mw1 DOUBLE PRECISION DEFAULT 0.0,
                    flex_p1 DOUBLE PRECISION DEFAULT 0.0,
                    flex_mw2 DOUBLE PRECISION DEFAULT 0.0,
                    flex_p2 DOUBLE PRECISION DEFAULT 0.0
                );
                """,
            )
        else:
            ejecutar_sql(
                cur,
                """
                CREATE TABLE IF NOT EXISTS dim_agentes_nacionales (
                    codigo_agente TEXT PRIMARY KEY,
                    nombre_agente TEXT,
                    activo INTEGER DEFAULT 1
                );
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
                );
                """,
            )
            ejecutar_sql(
                cur,
                """
                CREATE TABLE IF NOT EXISTS fact_declaraciones_contratos_envios (
                    id_envio INTEGER PRIMARY KEY AUTOINCREMENT,
                    fecha_operacion TEXT NOT NULL,
                    codigo_agente TEXT NOT NULL,
                    usuario_envio TEXT NOT NULL,
                    rol_envio TEXT NOT NULL,
                    nombre_archivo TEXT,
                    operador TEXT DEFAULT 'ENEE-HN',
                    fecha_archivo TEXT,
                    estado_vigencia TEXT NOT NULL DEFAULT 'VIGENTE',
                    total_filas INTEGER DEFAULT 0,
                    total_errores INTEGER DEFAULT 0,
                    total_advertencias INTEGER DEFAULT 0,
                    enviado_en TEXT NOT NULL
                );
                """,
            )
            ejecutar_sql(
                cur,
                """
                CREATE TABLE IF NOT EXISTS fact_declaraciones_contratos_detalle (
                    id_detalle INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_envio INTEGER NOT NULL,
                    fecha_operacion TEXT NOT NULL,
                    codigo_agente TEXT NOT NULL,
                    orden_fila INTEGER NOT NULL,
                    agente_i TEXT,
                    punto_med_i TEXT,
                    agente_r TEXT,
                    punto_med_r TEXT,
                    periodo_inicio INTEGER,
                    periodo_fin INTEGER,
                    tipo_contrato TEXT,
                    codigo_contrato_firme TEXT,
                    titular_dt TEXT,
                    responsable_transmision TEXT,
                    energia_declarada_mw REAL DEFAULT 0.0,
                    energia_requerida_mw REAL DEFAULT 0.0,
                    oferta_pmax_existe TEXT DEFAULT 'no',
                    pmax_mw1 REAL DEFAULT 0.0,
                    pmax_p1 REAL DEFAULT 0.0,
                    pmax_mw2 REAL DEFAULT 0.0,
                    pmax_p2 REAL DEFAULT 0.0,
                    pmax_mw3 REAL DEFAULT 0.0,
                    pmax_p3 REAL DEFAULT 0.0,
                    pmax_mw4 REAL DEFAULT 0.0,
                    pmax_p4 REAL DEFAULT 0.0,
                    pmax_mw5 REAL DEFAULT 0.0,
                    pmax_p5 REAL DEFAULT 0.0,
                    oferta_flex_existe TEXT DEFAULT 'no',
                    tipo_oferta_flexibilidad TEXT DEFAULT '',
                    flex_mw1 REAL DEFAULT 0.0,
                    flex_p1 REAL DEFAULT 0.0,
                    flex_mw2 REAL DEFAULT 0.0,
                    flex_p2 REAL DEFAULT 0.0
                );
                """,
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


def _obtener_agentes_nacionales_registrados(conn=None):
    nacionales = set()
    if conn is not None:
        _preparar_sesion_conn(conn)
        try:
            df_nac = ejecutar_consulta_df(
                "SELECT codigo_agente FROM dim_agentes_nacionales WHERE COALESCE(activo, 1) = 1",
                conn,
            )
            for v in df_nac["codigo_agente"].dropna().tolist():
                c = _limpiar_texto(v).upper()
                if c:
                    nacionales.add(c)
        except Exception:
            pass

        if not nacionales:
            try:
                df_cat = ejecutar_consulta_df("SELECT codigo_agente FROM dim_catalogo_agentes", conn)
                for v in df_cat["codigo_agente"].dropna().tolist():
                    c = _limpiar_texto(v).upper()
                    if c.startswith("3"):
                        nacionales.add(c)
            except Exception:
                pass

    if not nacionales:
        for k in DIRECTORIO_AGENTES_MER.keys():
            c = _limpiar_texto(k).upper()
            if c.startswith("3"):
                nacionales.add(c)

    return nacionales


def _obtener_mapa_contratos_firmes(conn=None):
    mapa = {}
    if conn is None:
        return mapa
    _preparar_sesion_conn(conn)
    try:
        df_cf = ejecutar_consulta_df(
            """
            SELECT codigo_contrato_firme, agente_autorizado, COALESCE(agente_contraparte, '') AS agente_contraparte
              FROM dim_contratos_firmes
             WHERE COALESCE(activo, 1) = 1
            """,
            conn,
        )
        for _, r in df_cf.iterrows():
            cod = _limpiar_texto(r.get("codigo_contrato_firme")).upper()
            ag_aut = _limpiar_texto(r.get("agente_autorizado")).upper()
            ag_cp = _limpiar_texto(r.get("agente_contraparte")).upper()
            if cod and ag_aut:
                mapa[cod] = {"agente_autorizado": ag_aut, "agente_contraparte": ag_cp}
    except Exception:
        pass
    return mapa


def _obtener_cf_vigentes_otros_agentes(conn, fecha_operacion_iso):
    """
    Devuelve {CODIGO_CONTRATO_FIRME_UPPER: AGENTE_QUE_LO_DECLARO_VIGENTE}
    para la fecha operativa indicada. La comparación contra el agente actual
    se realiza por fila/declarante para que el mismo agente pueda redeclarar
    sin bloquearse, mientras que otro agente sí queda bloqueado.
    """
    vigentes_por_cf = {}
    if conn is None or not fecha_operacion_iso:
        return vigentes_por_cf
    _preparar_sesion_conn(conn)
    try:
        df_v = ejecutar_consulta_df(
            """
            SELECT DISTINCT UPPER(TRIM(d.codigo_contrato_firme)) AS cod_cf,
                            UPPER(TRIM(e.codigo_agente)) AS ag_envio
              FROM fact_declaraciones_contratos_detalle d
              JOIN fact_declaraciones_contratos_envios e
                ON d.id_envio = e.id_envio
             WHERE d.fecha_operacion = ?
               AND e.estado_vigencia = 'VIGENTE'
               AND TRIM(COALESCE(d.codigo_contrato_firme, '')) != ''
            """,
            conn,
            params=[fecha_operacion_iso],
        )
        for _, r in df_v.iterrows():
            cod_cf = _limpiar_texto(r.get("cod_cf")).upper()
            ag_env = _limpiar_texto(r.get("ag_envio")).upper()
            if cod_cf and ag_env:
                vigentes_por_cf[cod_cf] = ag_env
    except Exception:
        pass
    return vigentes_por_cf


def _obtener_codigos_agentes_validos(conn=None):
    codigos = {_limpiar_texto(k).upper() for k in DIRECTORIO_AGENTES_MER.keys() if _limpiar_texto(k)}
    if conn is not None:
        _preparar_sesion_conn(conn)
        try:
            df = ejecutar_consulta_df("SELECT codigo_agente FROM dim_catalogo_agentes", conn)
            for v in df["codigo_agente"].dropna().tolist():
                c = _limpiar_texto(v).upper()
                if c:
                    codigos.add(c)
        except Exception:
            pass
    codigos.update(_obtener_agentes_nacionales_registrados(conn))
    return codigos


def _fila_vacia(reg):
    campos_clave = [
        reg.get("agente_i"),
        reg.get("punto_med_i"),
        reg.get("agente_r"),
        reg.get("punto_med_r"),
        reg.get("periodo_inicio"),
        reg.get("periodo_fin"),
        reg.get("tipo_contrato"),
        reg.get("energia_declarada_mw"),
        reg.get("energia_requerida_mw"),
    ]
    return all(_limpiar_texto(x) == "" for x in campos_clave)


def parsear_archivo_declaracion(file_bytes, nombre_archivo):
    nombre_low = (nombre_archivo or "").lower()
    hallazgos = []
    meta = {"operador": "", "fecha_archivo": ""}
    registros = []

    if nombre_low.endswith(".xml"):
        if len(file_bytes) > MAX_BYTES_XML:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": "-",
                "columna": "XML",
                "mensaje": "El archivo XML excede el tamaño máximo permitido de 10 MB.",
            })
            return meta, registros, hallazgos
        try:
            root = ET.fromstring(file_bytes)
        except Exception as exc:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": "-",
                "columna": "XML",
                "mensaje": f"No se pudo leer el archivo XML: {exc}",
            })
            return meta, registros, hallazgos

        total_nodos_xml = sum(1 for _ in root.iter())
        if total_nodos_xml > MAX_NODOS_XML:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": "-",
                "columna": "XML",
                "mensaje": f"El archivo XML excede el máximo permitido de {MAX_NODOS_XML} nodos.",
            })
            return meta, registros, hallazgos

        def _txt(parent, tag):
            if parent is None:
                return ""
            el = parent.find(f"{{{NS_EOR}}}{tag}")
            if el is None:
                el = parent.find(tag)
            return _limpiar_texto(el.text if el is not None else "")

        meta["operador"] = _txt(root, "Operador")
        meta["fecha_archivo"] = _normalizar_fecha_str(_txt(root, "Fecha"))

        decs = root.findall(f"{{{NS_EOR}}}Declaracion") or root.findall("Declaracion")
        for idx, dec in enumerate(decs, start=1):
            pmax_el = dec.find(f"{{{NS_EOR}}}OfertadePMAX")
            if pmax_el is None:
                pmax_el = dec.find("OfertadePMAX")
            flex_el = dec.find(f"{{{NS_EOR}}}OfertadeFLEX")
            if flex_el is None:
                flex_el = dec.find("OfertadeFLEX")

            reg = {
                "orden_fila": idx,
                "fila_origen": f"Declaración #{idx}",
                "agente_i": _txt(dec, "Agentei"),
                "punto_med_i": _txt(dec, "PuntoMedi"),
                "agente_r": _txt(dec, "Agenter"),
                "punto_med_r": _txt(dec, "PuntoMedr"),
                "periodo_inicio": _txt(dec, "PeriodoInicio"),
                "periodo_fin": _txt(dec, "PeriodoFin"),
                "tipo_contrato": _txt(dec, "TipoContrato"),
                "codigo_contrato_firme": _txt(dec, "CodigoContratoFirme"),
                "titular_dt": _txt(dec, "TitularDT"),
                "responsable_transmision": _txt(dec, "ResponsableTransmision"),
                "energia_declarada_mw": _txt(dec, "EnergiaDeclaradaMW"),
                "energia_requerida_mw": _txt(dec, "EnergiaRequeridaMW"),
                "oferta_pmax_existe": _limpiar_texto(pmax_el.attrib.get("Existe", "no")).lower() if pmax_el is not None else "no",
                "pmax_mw1": _txt(pmax_el, "MWBloque1"),
                "pmax_p1": _txt(pmax_el, "Precio1"),
                "pmax_mw2": _txt(pmax_el, "MWBloque2"),
                "pmax_p2": _txt(pmax_el, "Precio2"),
                "pmax_mw3": _txt(pmax_el, "MWBloque3"),
                "pmax_p3": _txt(pmax_el, "Precio3"),
                "pmax_mw4": _txt(pmax_el, "MWBloque4"),
                "pmax_p4": _txt(pmax_el, "Precio4"),
                "pmax_mw5": _txt(pmax_el, "MWBloque5"),
                "pmax_p5": _txt(pmax_el, "Precio5"),
                "oferta_flex_existe": _limpiar_texto(flex_el.attrib.get("Existe", "no")).lower() if flex_el is not None else "no",
                "tipo_oferta_flexibilidad": _txt(flex_el, "TipoOfertaFlexibilidad"),
                "flex_mw1": _txt(flex_el, "MWBloque1"),
                "flex_p1": _txt(flex_el, "Precio1"),
                "flex_mw2": _txt(flex_el, "MWBloque2"),
                "flex_p2": _txt(flex_el, "Precio2"),
            }
            if not _fila_vacia(reg):
                registros.append(reg)
        return meta, registros, hallazgos

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        hallazgos.append({
            "severidad": "ERROR",
            "fila": "-",
            "columna": "Excel",
            "mensaje": f"No se pudo abrir el libro Excel: {exc}",
        })
        return meta, registros, hallazgos

    nombre_hoja = None
    for sname in wb.sheetnames:
        if "declaraci" in sname.lower() and "contrato" in sname.lower():
            nombre_hoja = sname
            break
    if nombre_hoja is None:
        nombre_hoja = wb.sheetnames[0]
        hallazgos.append({
            "severidad": "ADVERTENCIA",
            "fila": "-",
            "columna": "Hoja",
            "mensaje": f"No se encontró hoja 'Declaración de contratos'; se leyó '{nombre_hoja}'.",
        })

    ws = wb[nombre_hoja]
    meta["operador"] = _limpiar_texto(ws["B4"].value)
    meta["fecha_archivo"] = _normalizar_fecha_str(ws["B5"].value)

    for r in range(8, ws.max_row + 1):
        reg = {
            "orden_fila": r - 7,
            "fila_origen": f"Fila {r}",
            "agente_i": _limpiar_texto(ws.cell(r, 1).value),
            "punto_med_i": _limpiar_texto(ws.cell(r, 2).value),
            "agente_r": _limpiar_texto(ws.cell(r, 3).value),
            "punto_med_r": _limpiar_texto(ws.cell(r, 4).value),
            "periodo_inicio": _limpiar_texto(ws.cell(r, 5).value),
            "periodo_fin": _limpiar_texto(ws.cell(r, 6).value),
            "tipo_contrato": _limpiar_texto(ws.cell(r, 7).value),
            "codigo_contrato_firme": _limpiar_texto(ws.cell(r, 8).value),
            "titular_dt": _limpiar_texto(ws.cell(r, 9).value),
            "responsable_transmision": _limpiar_texto(ws.cell(r, 10).value),
            "energia_declarada_mw": _limpiar_texto(ws.cell(r, 11).value),
            "energia_requerida_mw": _limpiar_texto(ws.cell(r, 12).value),
            "oferta_pmax_existe": (_limpiar_texto(ws.cell(r, 13).value) or "no").lower(),
            "pmax_mw1": _limpiar_texto(ws.cell(r, 14).value),
            "pmax_p1": _limpiar_texto(ws.cell(r, 15).value),
            "pmax_mw2": _limpiar_texto(ws.cell(r, 16).value),
            "pmax_p2": _limpiar_texto(ws.cell(r, 17).value),
            "pmax_mw3": _limpiar_texto(ws.cell(r, 18).value),
            "pmax_p3": _limpiar_texto(ws.cell(r, 19).value),
            "pmax_mw4": _limpiar_texto(ws.cell(r, 20).value),
            "pmax_p4": _limpiar_texto(ws.cell(r, 21).value),
            "pmax_mw5": _limpiar_texto(ws.cell(r, 22).value),
            "pmax_p5": _limpiar_texto(ws.cell(r, 23).value),
            "oferta_flex_existe": (_limpiar_texto(ws.cell(r, 24).value) or "no").lower(),
            "tipo_oferta_flexibilidad": _limpiar_texto(ws.cell(r, 25).value),
            "flex_mw1": _limpiar_texto(ws.cell(r, 26).value),
            "flex_p1": _limpiar_texto(ws.cell(r, 27).value),
            "flex_mw2": _limpiar_texto(ws.cell(r, 28).value),
            "flex_p2": _limpiar_texto(ws.cell(r, 29).value),
        }
        if not _fila_vacia(reg):
            registros.append(reg)

    return meta, registros, hallazgos


def validar_declaracion_contratos(
    meta,
    registros,
    fecha_operacion_esperada,
    codigo_agente_autorizado=None,
    es_admin=False,
    conn=None,
    hallazgos_previos=None,
):
    hallazgos = list(hallazgos_previos or [])
    if conn is not None:
        try:
            inicializar_tablas_declaraciones(conn)
        except Exception:
            pass

    agentes_validos = _obtener_codigos_agentes_validos(conn)
    agentes_nacionales = _obtener_agentes_nacionales_registrados(conn)
    mapa_cf = _obtener_mapa_contratos_firmes(conn)
    fecha_esp = _normalizar_fecha_str(fecha_operacion_esperada)
    ag_aut = _limpiar_texto(codigo_agente_autorizado).upper()
    cf_vigentes = _obtener_cf_vigentes_otros_agentes(conn, fecha_esp)

    if ag_aut and not es_admin:
        if not ag_aut.startswith("3"):
            hallazgos.append({
                "severidad": "ERROR",
                "fila": "Sesión",
                "columna": "Agente",
                "mensaje": f"El agente autorizado '{ag_aut}' no cumple con el prefijo nacional '3'.",
            })
        elif agentes_nacionales and ag_aut not in agentes_nacionales:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": "Sesión",
                "columna": "Agente",
                "mensaje": f"El agente nacional '{ag_aut}' no figura en la tabla de agentes nacionales registrados.",
            })

    operador = _limpiar_texto(meta.get("operador"))
    if operador and operador.upper() != "ENEE-HN":
        hallazgos.append({
            "severidad": "ADVERTENCIA",
            "fila": "Encabezado",
            "columna": "Operador",
            "mensaje": f"El operador del archivo es '{operador}' (se esperaba 'ENEE-HN').",
        })

    fecha_arch = _normalizar_fecha_str(meta.get("fecha_archivo"))
    if not fecha_arch:
        hallazgos.append({
            "severidad": "ERROR",
            "fila": "Encabezado",
            "columna": "Fecha",
            "mensaje": "Falta la fecha en el archivo o no tiene formato válido (DD/MM/YYYY).",
        })
    elif fecha_esp and fecha_arch != fecha_esp:
        hallazgos.append({
            "severidad": "ERROR",
            "fila": "Encabezado",
            "columna": "Fecha",
            "mensaje": f"La fecha del archivo ({fecha_arch}) no coincide con la Fecha Operativa seleccionada ({fecha_esp}).",
        })

    if not registros:
        hallazgos.append({
            "severidad": "ERROR",
            "fila": "-",
            "columna": "Registros",
            "mensaje": "El archivo no contiene filas de declaración de contratos.",
        })

    registros_limpios = []
    periodos_vistos = set()

    for reg in registros:
        f_lbl = reg["fila_origen"]
        ag_i = _limpiar_texto(reg.get("agente_i")).upper()
        pm_i = _limpiar_texto(reg.get("punto_med_i"))
        ag_r = _limpiar_texto(reg.get("agente_r")).upper()
        pm_r = _limpiar_texto(reg.get("punto_med_r"))
        tipo_ct = _limpiar_texto(reg.get("tipo_contrato")).upper()
        cod_cf = _limpiar_texto(reg.get("codigo_contrato_firme")).upper()

        # Determina el agente nacional declarante efectivo en esta fila
        declarante_fila = ag_aut
        if not declarante_fila:
            if ag_i.startswith("3"):
                declarante_fila = ag_i
            elif ag_r.startswith("3"):
                declarante_fila = ag_r

        if not ag_i:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Agente_i", "mensaje": "Agente_i vacío."})
        elif agentes_validos and ag_i not in agentes_validos:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Agente_i", "mensaje": f"Agente_i '{ag_i}' no reconocido en catálogo."})

        if not ag_r:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Agente_r", "mensaje": "Agente_r vacío."})
        elif agentes_validos and ag_r not in agentes_validos:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Agente_r", "mensaje": f"Agente_r '{ag_r}' no reconocido en catálogo."})

        if ag_i.startswith("3") and agentes_nacionales and ag_i not in agentes_nacionales:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": f_lbl,
                "columna": "Agente_i",
                "mensaje": f"Agente nacional '{ag_i}' no está registrado en dim_agentes_nacionales.",
            })
        if ag_r.startswith("3") and agentes_nacionales and ag_r not in agentes_nacionales:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": f_lbl,
                "columna": "Agente_r",
                "mensaje": f"Agente nacional '{ag_r}' no está registrado en dim_agentes_nacionales.",
            })

        if ag_i and ag_r and ag_i == ag_r:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Agente_i / Agente_r", "mensaje": "Agente_i y Agente_r no pueden ser iguales."})

        if not pm_i:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Punto_Med_i", "mensaje": "Punto_Med_i vacío."})
        if not pm_r:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Punto_Med_r", "mensaje": "Punto_Med_r vacío."})

        if ag_aut and not es_admin:
            if ag_aut not in (ag_i, ag_r):
                hallazgos.append({
                    "severidad": "ERROR",
                    "fila": f_lbl,
                    "columna": "Agente_i / Agente_r",
                    "mensaje": f"Acceso denegado: la fila ({ag_i}->{ag_r}) no corresponde al agente nacional autorizado ({ag_aut}).",
                })

        if tipo_ct == "CF" and not cod_cf:
            hallazgos.append({
                "severidad": "ERROR",
                "fila": f_lbl,
                "columna": "Codigo Contrato Firme",
                "mensaje": "El Tipo_Contrato es 'CF' pero falta el Codigo Contrato Firme.",
            })

        if cod_cf:
            if cod_cf in mapa_cf:
                titular_cf = mapa_cf[cod_cf]["agente_autorizado"]
                cp_cf = mapa_cf[cod_cf]["agente_contraparte"]

                if declarante_fila and declarante_fila != titular_cf:
                    hallazgos.append({
                        "severidad": "ERROR",
                        "fila": f_lbl,
                        "columna": "Codigo Contrato Firme",
                        "mensaje": (
                            f"Inconsistencia de titularidad: el Contrato Firme '{cod_cf}' pertenece "
                            f"exclusivamente a '{titular_cf}', no puede ser declarado por '{declarante_fila}'."
                        ),
                    })
                elif titular_cf not in (ag_i, ag_r):
                    hallazgos.append({
                        "severidad": "ERROR",
                        "fila": f_lbl,
                        "columna": "Codigo Contrato Firme",
                        "mensaje": (
                            f"El Contrato Firme '{cod_cf}' está asignado a '{titular_cf}', "
                            f"pero la fila declara {ag_i}->{ag_r}."
                        ),
                    })
                if cp_cf and cp_cf not in (ag_i, ag_r):
                    hallazgos.append({
                        "severidad": "ERROR",
                        "fila": f_lbl,
                        "columna": "Codigo Contrato Firme",
                        "mensaje": (
                            f"La contraparte registrada para '{cod_cf}' es '{cp_cf}', "
                            f"pero la fila declara {ag_i}->{ag_r}."
                        ),
                    })
            elif tipo_ct == "CF" and mapa_cf:
                hallazgos.append({
                    "severidad": "ERROR",
                    "fila": f_lbl,
                    "columna": "Codigo Contrato Firme",
                    "mensaje": f"El Codigo Contrato Firme '{cod_cf}' no está registrado en dim_contratos_firmes.",
                })

            # Solo bloquea si OTRO agente distinto ya lo tiene declarado como VIGENTE en la fecha
            ag_vigente_cf = cf_vigentes.get(cod_cf)
            if ag_vigente_cf and declarante_fila and ag_vigente_cf != declarante_fila:
                hallazgos.append({
                    "severidad": "ERROR",
                    "fila": f_lbl,
                    "columna": "Codigo Contrato Firme",
                    "mensaje": (
                        f"Duplicidad entre agentes: el Contrato Firme '{cod_cf}' ya fue declarado "
                        f"como VIGENTE por '{ag_vigente_cf}' para esta fecha operativa."
                    ),
                })

        p_ini, ok_pi = _a_int_periodo(reg.get("periodo_inicio"))
        p_fin, ok_pf = _a_int_periodo(reg.get("periodo_fin"))
        if not ok_pi:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Periodo_Inicio", "mensaje": "Periodo_Inicio debe ser un entero entre 0 y 23."})
        if not ok_pf:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Periodo_Fin", "mensaje": "Periodo_Fin debe ser un entero entre 0 y 23."})
        if ok_pi and ok_pf and p_ini > p_fin:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Periodo_Inicio / Periodo_Fin", "mensaje": f"Periodo_Inicio ({p_ini}) no puede ser mayor que Periodo_Fin ({p_fin})."})

        e_dec, ok_ed = _a_float(reg.get("energia_declarada_mw"), 0.0)
        e_req, ok_er = _a_float(reg.get("energia_requerida_mw"), 0.0)
        if not ok_ed or e_dec < 0:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Energia_Declarada MW", "mensaje": "Debe ser un número mayor o igual a 0."})
        if not ok_er or e_req < 0:
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Energia_Requerida MW", "mensaje": "Debe ser un número mayor o igual a 0."})

        pmax_ex = (_limpiar_texto(reg.get("oferta_pmax_existe")) or "no").lower()
        if pmax_ex not in ("si", "no"):
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Oferta de PMAX", "mensaje": "Debe ser 'si' o 'no'."})

        pmax_vals = []
        for idx_b in range(1, 6):
            mw_v, ok_mw = _a_float(reg.get(f"pmax_mw{idx_b}"), 0.0)
            pr_v, ok_pr = _a_float(reg.get(f"pmax_p{idx_b}"), 0.0)
            if not ok_mw or mw_v < 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": f"PMAX MW_Bloque{idx_b}", "mensaje": "Debe ser numérico >= 0."})
                mw_v = 0.0
            if not ok_pr or pr_v < 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": f"PMAX Precio{idx_b}", "mensaje": "Debe ser numérico >= 0."})
                pr_v = 0.0
            pmax_vals.append((mw_v, pr_v))

        if pmax_ex == "si":
            suma_pmax_mw = sum(x[0] for x in pmax_vals)
            if suma_pmax_mw <= 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Oferta de PMAX", "mensaje": "Oferta de PMAX marcada como 'si' pero todos los bloques MW están en 0."})
            for idx_b, (mw_v, pr_v) in enumerate(pmax_vals, start=1):
                if mw_v > 0 and pr_v <= 0:
                    hallazgos.append({"severidad": "ADVERTENCIA", "fila": f_lbl, "columna": f"PMAX Precio{idx_b}", "mensaje": f"Bloque PMAX {idx_b} tiene MW > 0 pero precio <= 0."})

        flex_ex = (_limpiar_texto(reg.get("oferta_flex_existe")) or "no").lower()
        if flex_ex not in ("si", "no"):
            hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Oferta de FLEX", "mensaje": "Debe ser 'si' o 'no'."})

        tipo_flex = _limpiar_texto(reg.get("tipo_oferta_flexibilidad"))
        flex_vals = []
        for idx_b in range(1, 3):
            mw_v, ok_mw = _a_float(reg.get(f"flex_mw{idx_b}"), 0.0)
            pr_v, ok_pr = _a_float(reg.get(f"flex_p{idx_b}"), 0.0)
            if not ok_mw or mw_v < 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": f"FLEX MW_Bloque{idx_b}", "mensaje": "Debe ser numérico >= 0."})
                mw_v = 0.0
            if not ok_pr or pr_v < 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": f"FLEX Precio{idx_b}", "mensaje": "Debe ser numérico >= 0."})
                pr_v = 0.0
            flex_vals.append((mw_v, pr_v))

        if flex_ex == "si":
            if not tipo_flex:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Tipo_Oferta_Flexibilidad", "mensaje": "Oferta de FLEX marcada como 'si' pero falta Tipo_Oferta_Flexibilidad."})
            suma_flex_mw = sum(x[0] for x in flex_vals)
            if suma_flex_mw <= 0:
                hallazgos.append({"severidad": "ERROR", "fila": f_lbl, "columna": "Oferta de FLEX", "mensaje": "Oferta de FLEX marcada como 'si' pero todos los bloques MW están en 0."})
            for idx_b, (mw_v, pr_v) in enumerate(flex_vals, start=1):
                if mw_v > 0 and pr_v <= 0:
                    hallazgos.append({"severidad": "ADVERTENCIA", "fila": f_lbl, "columna": f"FLEX Precio{idx_b}", "mensaje": f"Bloque FLEX {idx_b} tiene MW > 0 pero precio <= 0."})

        if ok_pi and ok_pf and p_ini <= p_fin:
            clave_base = (
                ag_i,
                pm_i,
                ag_r,
                pm_r,
                tipo_ct,
                cod_cf,
                pmax_ex,
                flex_ex,
                tipo_flex,
            )
            for hr in range(p_ini, p_fin + 1):
                clave_hr = clave_base + (hr,)
                if clave_hr in periodos_vistos:
                    hallazgos.append({
                        "severidad": "ERROR",
                        "fila": f_lbl,
                        "columna": "Periodo",
                        "mensaje": f"Traslape/duplicidad no permitida en periodo {hr} para {ag_i}->{ag_r} ({tipo_ct or 'sin tipo'}).",
                    })
                periodos_vistos.add(clave_hr)

        registros_limpios.append({
            "orden_fila": reg["orden_fila"],
            "agente_i": ag_i,
            "punto_med_i": pm_i,
            "agente_r": ag_r,
            "punto_med_r": pm_r,
            "periodo_inicio": p_ini if ok_pi else 0,
            "periodo_fin": p_fin if ok_pf else 0,
            "tipo_contrato": tipo_ct,
            "codigo_contrato_firme": cod_cf,
            "titular_dt": _limpiar_texto(reg.get("titular_dt")),
            "responsable_transmision": _limpiar_texto(reg.get("responsable_transmision")),
            "energia_declarada_mw": e_dec if ok_ed else 0.0,
            "energia_requerida_mw": e_req if ok_er else 0.0,
            "oferta_pmax_existe": pmax_ex,
            "pmax_mw1": pmax_vals[0][0],
            "pmax_p1": pmax_vals[0][1],
            "pmax_mw2": pmax_vals[1][0],
            "pmax_p2": pmax_vals[1][1],
            "pmax_mw3": pmax_vals[2][0],
            "pmax_p3": pmax_vals[2][1],
            "pmax_mw4": pmax_vals[3][0],
            "pmax_p4": pmax_vals[3][1],
            "pmax_mw5": pmax_vals[4][0],
            "pmax_p5": pmax_vals[4][1],
            "oferta_flex_existe": flex_ex,
            "tipo_oferta_flexibilidad": tipo_flex,
            "flex_mw1": flex_vals[0][0],
            "flex_p1": flex_vals[0][1],
            "flex_mw2": flex_vals[1][0],
            "flex_p2": flex_vals[1][1],
        })

    if not hallazgos:
        hallazgos_df = pd.DataFrame(columns=["severidad", "fila", "columna", "mensaje"])
    else:
        hallazgos_df = pd.DataFrame(hallazgos)

    bloqueado = any(h["severidad"] == "ERROR" for h in hallazgos)
    return registros_limpios, hallazgos_df, bloqueado


def guardar_declaracion_agente(
    conn,
    fecha_operacion,
    codigo_agente,
    usuario_envio,
    rol_envio,
    nombre_archivo,
    meta,
    registros_limpios,
    total_errores,
    total_advertencias,
):
    inicializar_tablas_declaraciones(conn)
    fecha_clean = _normalizar_fecha_str(fecha_operacion)
    ag_clean = _limpiar_texto(codigo_agente).upper()
    ahora_hn_str = datetime.now(ZONA_HN).strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.cursor()
    try:
        if es_postgres(conn):
            cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
            ejecutar_sql(
                cur,
                """
                UPDATE fact_declaraciones_contratos_envios
                   SET estado_vigencia = 'REEMPLAZADA'
                 WHERE fecha_operacion = ?
                   AND codigo_agente = ?
                   AND estado_vigencia = 'VIGENTE'
                """,
                (fecha_clean, ag_clean),
            )
            cur.execute(
                """
                INSERT INTO fact_declaraciones_contratos_envios (
                    fecha_operacion, codigo_agente, usuario_envio, rol_envio,
                    nombre_archivo, operador, fecha_archivo, estado_vigencia,
                    total_filas, total_errores, total_advertencias, enviado_en
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'VIGENTE', %s, %s, %s, %s)
                RETURNING id_envio
                """,
                (
                    fecha_clean,
                    ag_clean,
                    usuario_envio,
                    rol_envio,
                    nombre_archivo,
                    _limpiar_texto(meta.get("operador")) or "ENEE-HN",
                    _limpiar_texto(meta.get("fecha_archivo")),
                    len(registros_limpios),
                    int(total_errores),
                    int(total_advertencias),
                    ahora_hn_str,
                ),
            )
            id_envio = cur.fetchone()[0]
        else:
            ejecutar_sql(
                cur,
                """
                UPDATE fact_declaraciones_contratos_envios
                   SET estado_vigencia = 'REEMPLAZADA'
                 WHERE fecha_operacion = ?
                   AND codigo_agente = ?
                   AND estado_vigencia = 'VIGENTE'
                """,
                (fecha_clean, ag_clean),
            )
            ejecutar_sql(
                cur,
                """
                INSERT INTO fact_declaraciones_contratos_envios (
                    fecha_operacion, codigo_agente, usuario_envio, rol_envio,
                    nombre_archivo, operador, fecha_archivo, estado_vigencia,
                    total_filas, total_errores, total_advertencias, enviado_en
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'VIGENTE', ?, ?, ?, ?)
                """,
                (
                    fecha_clean,
                    ag_clean,
                    usuario_envio,
                    rol_envio,
                    nombre_archivo,
                    _limpiar_texto(meta.get("operador")) or "ENEE-HN",
                    _limpiar_texto(meta.get("fecha_archivo")),
                    len(registros_limpios),
                    int(total_errores),
                    int(total_advertencias),
                    ahora_hn_str,
                ),
            )
            cur.execute("SELECT last_insert_rowid()")
            id_envio = cur.fetchone()[0]

        for reg in registros_limpios:
            ejecutar_sql(
                cur,
                """
                INSERT INTO fact_declaraciones_contratos_detalle (
                    id_envio, fecha_operacion, codigo_agente, orden_fila,
                    agente_i, punto_med_i, agente_r, punto_med_r,
                    periodo_inicio, periodo_fin, tipo_contrato, codigo_contrato_firme,
                    titular_dt, responsable_transmision,
                    energia_declarada_mw, energia_requerida_mw,
                    oferta_pmax_existe,
                    pmax_mw1, pmax_p1, pmax_mw2, pmax_p2, pmax_mw3, pmax_p3,
                    pmax_mw4, pmax_p4, pmax_mw5, pmax_p5,
                    oferta_flex_existe, tipo_oferta_flexibilidad,
                    flex_mw1, flex_p1, flex_mw2, flex_p2
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    id_envio,
                    fecha_clean,
                    ag_clean,
                    reg["orden_fila"],
                    reg["agente_i"],
                    reg["punto_med_i"],
                    reg["agente_r"],
                    reg["punto_med_r"],
                    reg["periodo_inicio"],
                    reg["periodo_fin"],
                    reg["tipo_contrato"],
                    reg["codigo_contrato_firme"],
                    reg["titular_dt"],
                    reg["responsable_transmision"],
                    reg["energia_declarada_mw"],
                    reg["energia_requerida_mw"],
                    reg["oferta_pmax_existe"],
                    reg["pmax_mw1"],
                    reg["pmax_p1"],
                    reg["pmax_mw2"],
                    reg["pmax_p2"],
                    reg["pmax_mw3"],
                    reg["pmax_p3"],
                    reg["pmax_mw4"],
                    reg["pmax_p4"],
                    reg["pmax_mw5"],
                    reg["pmax_p5"],
                    reg["oferta_flex_existe"],
                    reg["tipo_oferta_flexibilidad"],
                    reg["flex_mw1"],
                    reg["flex_p1"],
                    reg["flex_mw2"],
                    reg["flex_p2"],
                ),
            )

        conn.commit()
        return id_envio
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


def obtener_envios_fecha(conn, fecha_operacion, codigo_agente=None):
    inicializar_tablas_declaraciones(conn)
    _preparar_sesion_conn(conn)
    fecha_clean = _normalizar_fecha_str(fecha_operacion)
    if codigo_agente:
        return ejecutar_consulta_df(
            """
            SELECT *
              FROM fact_declaraciones_contratos_envios
             WHERE fecha_operacion = ?
               AND codigo_agente = ?
             ORDER BY id_envio DESC
            """,
            conn,
            params=[fecha_clean, _limpiar_texto(codigo_agente).upper()],
        )
    return ejecutar_consulta_df(
        """
        SELECT *
          FROM fact_declaraciones_contratos_envios
         WHERE fecha_operacion = ?
         ORDER BY codigo_agente ASC, id_envio DESC
        """,
        conn,
        params=[fecha_clean],
    )


def obtener_detalle_consolidado_fecha(conn, fecha_operacion):
    inicializar_tablas_declaraciones(conn)
    _preparar_sesion_conn(conn)
    fecha_clean = _normalizar_fecha_str(fecha_operacion)
    return ejecutar_consulta_df(
        """
        SELECT d.*, e.usuario_envio, e.nombre_archivo, e.enviado_en
          FROM fact_declaraciones_contratos_detalle d
          JOIN fact_declaraciones_contratos_envios e
            ON d.id_envio = e.id_envio
         WHERE d.fecha_operacion = ?
           AND e.estado_vigencia = 'VIGENTE'
         ORDER BY d.codigo_agente ASC, d.id_detalle ASC
        """,
        conn,
        params=[fecha_clean],
    )


def generar_excel_consolidado_eor(df_detalle, fecha_operacion, ruta_plantilla="HON20261129.xlsm"):
    fecha_clean = _normalizar_fecha_str(fecha_operacion)
    fecha_eor = _fecha_a_ddmmyyyy(fecha_clean)

    if ruta_plantilla and os.path.exists(ruta_plantilla):
        wb = openpyxl.load_workbook(ruta_plantilla)
        nombre_hoja = None
        for sname in wb.sheetnames:
            if "declaraci" in sname.lower() and "contrato" in sname.lower():
                nombre_hoja = sname
                break
        ws = wb[nombre_hoja or wb.sheetnames[0]]
        if ws.max_row >= 8:
            ws.delete_rows(8, ws.max_row - 7)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Declaración de contratos"
        ws["A1"] = "Ente Operador Regional"
        ws["A2"] = "Declaración de Contratos del MER"
        ws["A4"] = "Operador de Mercado"
        ws["A5"] = "Fecha"
        for col_idx, col_name in enumerate(COLUMNAS_PLANTILLA_EOR, start=1):
            ws.cell(7, col_idx, col_name)

    ws["B4"] = "ENEE-HN"
    ws["B5"] = fecha_eor

    if df_detalle is not None and not df_detalle.empty:
        fila_excel = 8
        for _, r in df_detalle.iterrows():
            valores = [
                _limpiar_texto(r.get("agente_i")),
                _limpiar_texto(r.get("punto_med_i")),
                _limpiar_texto(r.get("agente_r")),
                _limpiar_texto(r.get("punto_med_r")),
                int(r.get("periodo_inicio") or 0),
                int(r.get("periodo_fin") or 0),
                _limpiar_texto(r.get("tipo_contrato")),
                _limpiar_texto(r.get("codigo_contrato_firme")) or None,
                _limpiar_texto(r.get("titular_dt")),
                _limpiar_texto(r.get("responsable_transmision")),
                float(r.get("energia_declarada_mw") or 0.0),
                float(r.get("energia_requerida_mw") or 0.0),
                (_limpiar_texto(r.get("oferta_pmax_existe")) or "no").lower(),
                float(r.get("pmax_mw1") or 0.0),
                float(r.get("pmax_p1") or 0.0),
                float(r.get("pmax_mw2") or 0.0),
                float(r.get("pmax_p2") or 0.0),
                float(r.get("pmax_mw3") or 0.0),
                float(r.get("pmax_p3") or 0.0),
                float(r.get("pmax_mw4") or 0.0),
                float(r.get("pmax_p4") or 0.0),
                float(r.get("pmax_mw5") or 0.0),
                float(r.get("pmax_p5") or 0.0),
                (_limpiar_texto(r.get("oferta_flex_existe")) or "no").lower(),
                _limpiar_texto(r.get("tipo_oferta_flexibilidad")),
                float(r.get("flex_mw1") or 0.0),
                float(r.get("flex_p1") or 0.0),
                float(r.get("flex_mw2") or 0.0),
                float(r.get("flex_p2") or 0.0),
            ]
            for c_idx, val in enumerate(valores, start=1):
                if val is not None:
                    ws.cell(fila_excel, c_idx, val)
            fila_excel += 1

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out.getvalue()


def generar_xml_consolidado_eor(df_detalle, fecha_operacion, operador="ENEE-HN"):
    fecha_clean = _normalizar_fecha_str(fecha_operacion)
    fecha_eor = _fecha_a_ddmmyyyy(fecha_clean)

    root = ET.Element("DeclaracionDeContratos", {"xmlns": NS_EOR})
    ET.SubElement(root, "Operador").text = _limpiar_texto(operador) or "ENEE-HN"
    ET.SubElement(root, "Fecha").text = fecha_eor

    def _fmt_num(val):
        f = float(val or 0.0)
        if f.is_integer():
            return str(int(f))
        return f"{f:g}"

    if df_detalle is not None and not df_detalle.empty:
        for _, r in df_detalle.iterrows():
            dec = ET.SubElement(root, "Declaracion")
            ET.SubElement(dec, "Agentei").text = _limpiar_texto(r.get("agente_i"))
            ET.SubElement(dec, "PuntoMedi").text = _limpiar_texto(r.get("punto_med_i"))
            ET.SubElement(dec, "Agenter").text = _limpiar_texto(r.get("agente_r"))
            ET.SubElement(dec, "PuntoMedr").text = _limpiar_texto(r.get("punto_med_r"))
            ET.SubElement(dec, "PeriodoInicio").text = str(int(r.get("periodo_inicio") or 0))
            ET.SubElement(dec, "PeriodoFin").text = str(int(r.get("periodo_fin") or 0))
            ET.SubElement(dec, "TipoContrato").text = _limpiar_texto(r.get("tipo_contrato"))

            cod_firme = _limpiar_texto(r.get("codigo_contrato_firme"))
            if cod_firme:
                ET.SubElement(dec, "CodigoContratoFirme").text = cod_firme

            ET.SubElement(dec, "TitularDT").text = _limpiar_texto(r.get("titular_dt"))
            ET.SubElement(dec, "ResponsableTransmision").text = _limpiar_texto(r.get("responsable_transmision"))
            ET.SubElement(dec, "EnergiaDeclaradaMW").text = _fmt_num(r.get("energia_declarada_mw"))
            ET.SubElement(dec, "EnergiaRequeridaMW").text = _fmt_num(r.get("energia_requerida_mw"))

            pmax_ex = (_limpiar_texto(r.get("oferta_pmax_existe")) or "no").lower()
            pmax_el = ET.SubElement(dec, "OfertadePMAX", {"Existe": pmax_ex})
            for b in range(1, 6):
                ET.SubElement(pmax_el, f"MWBloque{b}").text = _fmt_num(r.get(f"pmax_mw{b}"))
                ET.SubElement(pmax_el, f"Precio{b}").text = _fmt_num(r.get(f"pmax_p{b}"))

            flex_ex = (_limpiar_texto(r.get("oferta_flex_existe")) or "no").lower()
            flex_el = ET.SubElement(dec, "OfertadeFLEX", {"Existe": flex_ex})
            ET.SubElement(flex_el, "TipoOfertaFlexibilidad").text = _limpiar_texto(r.get("tipo_oferta_flexibilidad"))
            for b in range(1, 3):
                ET.SubElement(flex_el, f"MWBloque{b}").text = _fmt_num(r.get(f"flex_mw{b}"))
                ET.SubElement(flex_el, f"Precio{b}").text = _fmt_num(r.get(f"flex_p{b}"))

    xml_str = ET.tostring(root, encoding="iso-8859-1", xml_declaration=True)
    return xml_str
