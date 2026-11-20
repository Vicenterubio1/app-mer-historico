import os
import io
import re
import zipfile
import hashlib
from datetime import datetime
import pandas as pd

from db_mer import (
    obtener_conexion,
    inicializar_esquema_completo,
    ejecutar_sql,
    insertar_lote,
    es_postgres,
    ESQUEMA_PG,
)

MAPA_OSOM_PAIS = {
    "OSO001": "GT",
    "OSO002": "SV",
    "OSO003": "HN",
    "OSO004": "NI",
    "OSO005": "CR",
    "OSO006": "PA",
}

MAPA_OSOM_NOMBRE = {
    "OSO001": "AMM (Guatemala)",
    "OSO002": "UT (El Salvador)",
    "OSO003": "CND (Honduras)",
    "OSO004": "CNDC (Nicaragua)",
    "OSO005": "CENCE (Costa Rica)",
    "OSO006": "CND (Panamá)",
}

PREFIJO_PAIS = {
    "1": "GT",
    "2": "SV",
    "3": "HN",
    "4": "NI",
    "5": "CR",
    "6": "PA",
}


def limpiar_codigo(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def calcular_hash_bytes(data_bytes):
    return hashlib.sha256(data_bytes).hexdigest()


def asegurar_columna_tipo_oferta(conn):
    """Agrega la columna tipo_oferta a fact_contratos_tcp si aún no existe (SQLite y PostgreSQL)."""
    cur = conn.cursor()
    try:
        if es_postgres(conn):
            cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
            cur.execute("ALTER TABLE fact_contratos_tcp ADD COLUMN IF NOT EXISTS tipo_oferta TEXT DEFAULT '';")
        else:
            cur.execute("PRAGMA table_info(fact_contratos_tcp);")
            cols = [r[1].lower() for r in cur.fetchall()]
            if "tipo_oferta" not in cols:
                cur.execute("ALTER TABLE fact_contratos_tcp ADD COLUMN tipo_oferta TEXT DEFAULT '';")
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()


def inicializar_bd(conn):
    inicializar_esquema_completo(conn)
    asegurar_columna_tipo_oferta(conn)
    cur = conn.cursor()
    if es_postgres(conn):
        cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fact_flujos_rtr (
                id SERIAL PRIMARY KEY,
                fecha_operacion TEXT NOT NULL,
                fecha_hora TEXT NOT NULL,
                bus_i TEXT,
                bus_j TEXT,
                ckt TEXT,
                flujo_mer_mw DOUBLE PRECISION,
                perdidas_mer_mw DOUBLE PRECISION,
                flujo_nacional_mw DOUBLE PRECISION
            );
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fact_flujos_rtr (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_operacion TEXT NOT NULL,
                fecha_hora TEXT NOT NULL,
                bus_i TEXT,
                bus_j TEXT,
                ckt TEXT,
                flujo_mer_mw REAL,
                perdidas_mer_mw REAL,
                flujo_nacional_mw REAL
            );
        """)
    conn.commit()
    cur.close()


def extraer_metadatos_nombre(nombre_archivo):
    """Extrae fecha_operacion (YYYY-MM-DD), codigo_osom (OSOXXX) y pais del nombre PUB004-PRE-YYYYMMDD-OSOXXX-..."""
    m = re.search(r"PUB004-PRE-(\d{8})-(OSO\d{3})", nombre_archivo, re.IGNORECASE)
    if not m:
        return None, None, None
    f_raw = m.group(1)
    osom = m.group(2).upper()
    fecha_op = f"{f_raw[:4]}-{f_raw[4:6]}-{f_raw[6:8]}"
    pais = MAPA_OSOM_PAIS.get(osom, "OTRO")
    return fecha_op, osom, pais


def obtener_timestamp_real_eor(zip_info, nombre_interno):
    """Obtiene la fecha y hora real de creación/modificación del Excel dentro del ZIP."""
    try:
        dt = datetime(*zip_info.date_time)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def detectar_fila_encabezado(df_raw, palabras_clave):
    for idx in range(min(25, len(df_raw))):
        fila_txt = " ".join(str(x).strip().lower() for x in df_raw.iloc[idx].tolist() if pd.notna(x))
        if all(pk.lower() in fila_txt for pk in palabras_clave):
            return idx
    return None


def normalizar_hora(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if ":" in s:
        s = s.split(":")[0]
    try:
        h = int(float(s))
        if 0 <= h <= 23:
            return f"{h:02d}"
    except Exception:
        pass
    return None


def parsear_hoja_pexante(xl, fecha_op):
    hoja = next((h for h in xl.sheet_names if "PEXANTE" in h.upper()), None)
    if not hoja:
        return pd.DataFrame()

    df_raw = pd.read_excel(xl, sheet_name=hoja, header=None)
    hdr_idx = detectar_fila_encabezado(df_raw, ["nodo"])
    if hdr_idx is None:
        return pd.DataFrame()

    df = pd.read_excel(xl, sheet_name=hoja, header=hdr_idx)
    df.columns = [str(c).strip() for c in df.columns]

    col_nodo = next((c for c in df.columns if "nodo" in c.lower()), None)
    if not col_nodo:
        return pd.DataFrame()

    registros = []
    cols_horas = {}
    for c in df.columns:
        h_norm = normalizar_hora(c)
        if h_norm is not None and c != col_nodo:
            cols_horas[c] = h_norm

    if len(cols_horas) >= 12:
        for _, row in df.iterrows():
            nodo = limpiar_codigo(row[col_nodo])
            if not nodo or not nodo[0].isdigit():
                continue
            for col_orig, h_str in cols_horas.items():
                val = pd.to_numeric(row[col_orig], errors="coerce")
                if pd.notna(val):
                    registros.append({
                        "fecha_operacion": fecha_op,
                        "fecha_hora": f"{fecha_op} {h_str}:00:00",
                        "codigo_nodo": nodo,
                        "precio_exante_usd": float(val),
                    })
    else:
        col_per = next((c for c in df.columns if "periodo" in c.lower() or "hora" in c.lower()), None)
        col_pre = next((c for c in df.columns if "precio" in c.lower() or "exante" in c.lower()), None)
        if col_per and col_pre:
            for _, row in df.iterrows():
                nodo = limpiar_codigo(row[col_nodo])
                h_str = normalizar_hora(row[col_per])
                val = pd.to_numeric(row[col_pre], errors="coerce")
                if nodo and nodo[0].isdigit() and h_str and pd.notna(val):
                    registros.append({
                        "fecha_operacion": fecha_op,
                        "fecha_hora": f"{fecha_op} {h_str}:00:00",
                        "codigo_nodo": nodo,
                        "precio_exante_usd": float(val),
                    })

    return pd.DataFrame(registros)


def parsear_hoja_tcp(xl, fecha_op, pais):
    hoja = next((h for h in xl.sheet_names if h.strip().upper() == "TCP" or "TCP" in h.upper()), None)
    if not hoja:
        return pd.DataFrame()

    df_raw = pd.read_excel(xl, sheet_name=hoja, header=None)
    hdr_idx = detectar_fila_encabezado(df_raw, ["periodo", "agente"])
    if hdr_idx is None:
        return pd.DataFrame()

    df = pd.read_excel(xl, sheet_name=hoja, header=hdr_idx)
    df.columns = [str(c).strip() for c in df.columns]

    def buscar_col(candidatos):
        for cand in candidatos:
            for c in df.columns:
                if cand in c.lower():
                    return c
        return None

    c_per = buscar_col(["periodo", "hora"])
    c_nodo = buscar_col(["nodo"])
    c_ag = next((c for c in df.columns if "agente" in c.lower() and "contra" not in c.lower()), None)
    c_pm = next((c for c in df.columns if "punto" in c.lower() and "contra" not in c.lower()), None)
    c_cont = buscar_col(["código contrato", "codigo contrato", "contrato"])
    c_cp = buscar_col(["contraparte"])
    c_tipo_tr = buscar_col(["tipo transa", "transacción", "transaccion"])
    c_tipo_ct = buscar_col(["tipo contrato"])
    c_tipo_of = buscar_col(["tipo oferta", "oferta"])
    # Respaldo posicional por si la columna L tiene encabezado abreviado
    if not c_tipo_of and len(df.columns) > 11:
        c_tipo_of = df.columns[11]

    c_req = buscar_col(["requerida"])
    c_dec = buscar_col(["declarada"])
    c_pre = buscar_col(["precio"])
    c_pred = buscar_col(["predespachad"])

    if not (c_per and c_ag and c_tipo_tr and c_pred):
        return pd.DataFrame()

    registros = []
    for _, row in df.iterrows():
        h_str = normalizar_hora(row[c_per])
        ag = limpiar_codigo(row[c_ag])
        tipo_tr = str(row[c_tipo_tr] or "").strip().lower()
        if not h_str or not ag or tipo_tr not in ("i", "r"):
            continue

        val_of = row[c_tipo_of] if c_tipo_of else ""
        tipo_of_txt = "" if pd.isna(val_of) else str(val_of).strip()

        registros.append({
            "fecha_operacion": fecha_op,
            "fecha_hora": f"{fecha_op} {h_str}:00:00",
            "pais": pais,
            "codigo_nodo": limpiar_codigo(row[c_nodo]) if c_nodo else "",
            "codigo_agente": ag,
            "punto_medida": str(row[c_pm] or "").strip() if c_pm else "",
            "codigo_contrato": str(row[c_cont] or "").strip() if c_cont else "",
            "tipo_contrato": str(row[c_tipo_ct] or "").strip() if c_tipo_ct else "",
            "tipo_oferta": tipo_of_txt,
            "agente_contraparte": str(row[c_cp] or "").strip() if c_cp else "",
            "tipo_transaccion": tipo_tr,
            "mw_requerida": float(pd.to_numeric(row[c_req], errors="coerce") or 0.0) if c_req else 0.0,
            "mw_declarada": float(pd.to_numeric(row[c_dec], errors="coerce") or 0.0) if c_dec else 0.0,
            "precio_exante": float(pd.to_numeric(row[c_pre], errors="coerce") or 0.0) if c_pre else 0.0,
            "mw_predespachados": float(pd.to_numeric(row[c_pred], errors="coerce") or 0.0),
        })

    return pd.DataFrame(registros)


def parsear_hoja_top(xl, fecha_op, pais):
    hoja = next((h for h in xl.sheet_names if h.strip().upper() == "TOP" or "TOP" in h.upper()), None)
    if not hoja:
        return pd.DataFrame()

    df_raw = pd.read_excel(xl, sheet_name=hoja, header=None)
    hdr_idx = detectar_fila_encabezado(df_raw, ["periodo", "agente"])
    if hdr_idx is None:
        return pd.DataFrame()

    df = pd.read_excel(xl, sheet_name=hoja, header=hdr_idx)
    df.columns = [str(c).strip() for c in df.columns]

    def buscar_col(candidatos):
        for cand in candidatos:
            for c in df.columns:
                if cand in c.lower():
                    return c
        return None

    c_per = buscar_col(["periodo", "hora"])
    c_nodo = buscar_col(["nodo"])
    c_ag = buscar_col(["agente"])
    c_pm = buscar_col(["punto"])
    c_tipo_tr = buscar_col(["tipo transa", "transacción", "transaccion"])
    c_req = buscar_col(["requerida"])
    c_dec = buscar_col(["declarada"])
    c_pre = buscar_col(["precio"])
    c_pred = buscar_col(["predespachad"])

    if not (c_per and c_ag and c_tipo_tr and c_pred):
        return pd.DataFrame()

    registros = []
    for _, row in df.iterrows():
        h_str = normalizar_hora(row[c_per])
        ag = limpiar_codigo(row[c_ag])
        tipo_tr = str(row[c_tipo_tr] or "").strip().lower()
        if not h_str or not ag or tipo_tr not in ("i", "r"):
            continue

        registros.append({
            "fecha_operacion": fecha_op,
            "fecha_hora": f"{fecha_op} {h_str}:00:00",
            "pais": pais,
            "codigo_nodo": limpiar_codigo(row[c_nodo]) if c_nodo else "",
            "codigo_agente": ag,
            "punto_medida": str(row[c_pm] or "").strip() if c_pm else "",
            "tipo_transaccion": tipo_tr,
            "mw_requerida": float(pd.to_numeric(row[c_req], errors="coerce") or 0.0) if c_req else 0.0,
            "mw_declarada": float(pd.to_numeric(row[c_dec], errors="coerce") or 0.0) if c_dec else 0.0,
            "precio_exante": float(pd.to_numeric(row[c_pre], errors="coerce") or 0.0) if c_pre else 0.0,
            "mw_predespachados": float(pd.to_numeric(row[c_pred], errors="coerce") or 0.0),
        })

    return pd.DataFrame(registros)


def parsear_hoja_rtr(xl, fecha_op):
    hoja = next((h for h in xl.sheet_names if "RTR" in h.upper()), None)
    if not hoja:
        return pd.DataFrame()

    df_raw = pd.read_excel(xl, sheet_name=hoja, header=None)
    hdr_idx = detectar_fila_encabezado(df_raw, ["periodo"])
    if hdr_idx is None:
        return pd.DataFrame()

    df = pd.read_excel(xl, sheet_name=hoja, header=hdr_idx)
    df.columns = [str(c).strip() for c in df.columns]

    c_per = next((c for c in df.columns if "periodo" in c.lower() or "hora" in c.lower()), None)
    c_bi = next((c for c in df.columns if "bus_i" in c.lower() or "bus i" in c.lower() or "desde" in c.lower()), None)
    c_bj = next((c for c in df.columns if "bus_j" in c.lower() or "bus j" in c.lower() or "hasta" in c.lower()), None)
    c_ckt = next((c for c in df.columns if "ckt" in c.lower() or "circuito" in c.lower()), None)
    c_fmer = next((c for c in df.columns if "flujo" in c.lower() and "mer" in c.lower()), None)
    c_pmer = next((c for c in df.columns if "perdida" in c.lower() or "pérdida" in c.lower()), None)
    c_fnac = next((c for c in df.columns if "nacional" in c.lower()), None)

    if not (c_per and c_fmer):
        return pd.DataFrame()

    registros = []
    for _, row in df.iterrows():
        h_str = normalizar_hora(row[c_per])
        f_mer = pd.to_numeric(row[c_fmer], errors="coerce")
        if not h_str or pd.isna(f_mer):
            continue

        registros.append({
            "fecha_operacion": fecha_op,
            "fecha_hora": f"{fecha_op} {h_str}:00:00",
            "bus_i": limpiar_codigo(row[c_bi]) if c_bi else "",
            "bus_j": limpiar_codigo(row[c_bj]) if c_bj else "",
            "ckt": str(row[c_ckt] or "").strip() if c_ckt else "",
            "flujo_mer_mw": float(f_mer),
            "perdidas_mer_mw": float(pd.to_numeric(row[c_pmer], errors="coerce") or 0.0) if c_pmer else 0.0,
            "flujo_nacional_mw": float(pd.to_numeric(row[c_fnac], errors="coerce") or 0.0) if c_fnac else 0.0,
        })

    return pd.DataFrame(registros)


def _insertar_df_en_tabla(cur, tabla, df):
    if df.empty:
        return
    cols = list(df.columns)
    filas = [tuple(None if pd.isna(x) else x for x in r) for r in df[cols].to_numpy()]
    insertar_lote(cur, tabla, cols, filas)


def procesar_excel_en_memoria(conn, nombre_excel, contenido_bytes, ts_publicacion_eor):
    fecha_op, osom, pais = extraer_metadatos_nombre(nombre_excel)
    if not fecha_op or not osom:
        print(f"[IGNORADO] Nombre no reconocido: {nombre_excel}")
        return

    hash_actual = calcular_hash_bytes(contenido_bytes)
    cur = conn.cursor()
    ejecutar_sql(
        cur,
        """
        SELECT archivo_origen, hash_archivo, fecha_publicacion_eor
        FROM dim_archivos_cargados
        WHERE fecha_operacion = ? AND codigo_osom = ?
        """,
        (fecha_op, osom),
    )
    existente = cur.fetchone()

    xl = None
    df_tcp_pre = None

    if existente:
        arch_prev, hash_prev, ts_prev = existente
        if hash_prev == hash_actual:
            # Verificar si faltaba poblar tipo_oferta en registros cargados antes de la actualización
            ejecutar_sql(
                cur,
                """
                SELECT COUNT(*) FROM fact_contratos_tcp
                WHERE fecha_operacion = ? AND pais = ? AND COALESCE(TRIM(tipo_oferta), '') != ''
                """,
                (fecha_op, pais),
            )
            tiene_of = (cur.fetchone()[0] or 0) > 0
            if tiene_of:
                print(f"[SKIP] {nombre_excel} (idéntico al ya cargado)")
                cur.close()
                return
            xl = pd.ExcelFile(io.BytesIO(contenido_bytes))
            df_tcp_pre = parsear_hoja_tcp(xl, fecha_op, pais)
            if df_tcp_pre.empty or (df_tcp_pre["tipo_oferta"].str.strip() == "").all():
                print(f"[SKIP] {nombre_excel} (idéntico al ya cargado)")
                cur.close()
                return
            print(f"[UPDATE - Columna Tipo Oferta] Completando Tipo Oferta en {fecha_op} {pais} ({osom})...")
        elif ts_prev and ts_publicacion_eor and ts_publicacion_eor < ts_prev:
            print(f"[SKIP - Versión antigua] {nombre_excel} ({ts_publicacion_eor}) es anterior a {arch_prev} ({ts_prev})")
            cur.close()
            return
        else:
            print(f"[UPDATE] Actualizando {fecha_op} {pais} ({osom}) con versión más reciente ({ts_publicacion_eor})...")
    else:
        print(f"[NUEVO] Cargando {fecha_op} {pais} ({osom}) - Publicación EOR: {ts_publicacion_eor}")

    if xl is None:
        xl = pd.ExcelFile(io.BytesIO(contenido_bytes))

    df_pex = parsear_hoja_pexante(xl, fecha_op)
    df_tcp = df_tcp_pre if df_tcp_pre is not None else parsear_hoja_tcp(xl, fecha_op, pais)
    df_top = parsear_hoja_top(xl, fecha_op, pais)
    df_rtr = parsear_hoja_rtr(xl, fecha_op)

    ejecutar_sql(cur, "DELETE FROM fact_contratos_tcp WHERE fecha_operacion = ? AND pais = ?", (fecha_op, pais))
    ejecutar_sql(cur, "DELETE FROM fact_oportunidad_top WHERE fecha_operacion = ? AND pais = ?", (fecha_op, pais))

    if not df_pex.empty:
        df_pex = df_pex.drop_duplicates(subset=["fecha_operacion", "fecha_hora", "codigo_nodo"], keep="last")
        nodos_unicos = df_pex["codigo_nodo"].unique().tolist()
        if nodos_unicos:
            placeholders = ", ".join(["?"] * len(nodos_unicos))
            ejecutar_sql(
                cur,
                f"DELETE FROM fact_precios_exante WHERE fecha_operacion = ? AND codigo_nodo IN ({placeholders})",
                [fecha_op] + nodos_unicos,
            )
        _insertar_df_en_tabla(cur, "fact_precios_exante", df_pex)

    if not df_tcp.empty:
        _insertar_df_en_tabla(cur, "fact_contratos_tcp", df_tcp)

    if not df_top.empty:
        _insertar_df_en_tabla(cur, "fact_oportunidad_top", df_top)

    if not df_rtr.empty:
        ejecutar_sql(cur, "DELETE FROM fact_flujos_rtr WHERE fecha_operacion = ?", (fecha_op,))
        _insertar_df_en_tabla(cur, "fact_flujos_rtr", df_rtr)

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nombre_osom = MAPA_OSOM_NOMBRE.get(osom, osom)
    ejecutar_sql(
        cur,
        """
        INSERT INTO dim_archivos_cargados (
            fecha_operacion, codigo_osom, pais, nombre_osom, archivo_origen,
            timestamp_version, fecha_carga, fecha_publicacion_eor, hash_archivo
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fecha_operacion, codigo_osom) DO UPDATE SET
            nombre_osom = excluded.nombre_osom,
            archivo_origen = excluded.archivo_origen,
            timestamp_version = excluded.timestamp_version,
            fecha_carga = excluded.fecha_carga,
            fecha_publicacion_eor = excluded.fecha_publicacion_eor,
            hash_archivo = excluded.hash_archivo
        """,
        (fecha_op, osom, pais, nombre_osom, nombre_excel, ts_publicacion_eor, ahora, ts_publicacion_eor, hash_actual),
    )

    conn.commit()
    cur.close()


def procesar_carpeta(ruta_carpeta="."):
    conn = obtener_conexion()
    inicializar_bd(conn)

    archivos_zip = sorted([
        os.path.join(ruta_carpeta, f)
        for f in os.listdir(ruta_carpeta)
        if f.lower().endswith(".zip") and "PUB004" in f.upper()
    ])

    for ruta_zip in archivos_zip:
        try:
            with zipfile.ZipFile(ruta_zip, "r") as zf:
                for info in zf.infolist():
                    if info.filename.lower().endswith(".xlsx") and not info.filename.startswith("__MACOSX"):
                        nombre_interno = os.path.basename(info.filename)
                        ts_eor = obtener_timestamp_real_eor(info, nombre_interno)
                        contenido = zf.read(info.filename)
                        procesar_excel_en_memoria(conn, nombre_interno, contenido, ts_eor)
        except Exception as e:
            print(f"[ERROR] No se pudo procesar {ruta_zip}: {e}")

    conn.close()


if __name__ == "__main__":
    procesar_carpeta(".")﻿
