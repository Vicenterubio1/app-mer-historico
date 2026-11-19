import os
import sqlite3
import pandas as pd

try:
    import streamlit as st
except ImportError:
    st = None

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

SQLITE_DB_PATH = "historico_mer.db"
ESQUEMA_PG = "mer"


def obtener_database_url() -> str:
    """Obtiene la URI de PostgreSQL desde st.secrets o variable de entorno."""
    url = ""
    if st is not None:
        try:
            if "DATABASE_URL" in st.secrets:
                url = str(st.secrets["DATABASE_URL"]).strip()
        except Exception:
            pass
    if not url:
        url = os.getenv("DATABASE_URL", "").strip()
    return url


def es_postgres(conn=None) -> bool:
    """Detecta si se trabaja con PostgreSQL por la conexión activa o por configuración."""
    if conn is not None:
        return not isinstance(conn, sqlite3.Connection)
    url = obtener_database_url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def obtener_conexion():
    """Devuelve una conexión activa a PostgreSQL (esquema 'mer') o SQLite local."""
    url = obtener_database_url()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        if psycopg2 is None:
            raise ImportError("Falta instalar 'psycopg2-binary' para conectar a PostgreSQL.")
        if "sslmode=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode=require"
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ESQUEMA_PG};")
        cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
        conn.commit()
        cur.close()
        return conn
    return sqlite3.connect(SQLITE_DB_PATH)


def ejecutar_consulta_df(sql: str, conn, params=None) -> pd.DataFrame:
    """Ejecuta SELECT adaptando placeholders (? -> %s) y sintaxis de fecha según el motor."""
    pg = es_postgres(conn)
    sql_adaptado = sql
    if pg:
        sql_adaptado = sql_adaptado.replace("?", "%s")
        sql_adaptado = sql_adaptado.replace("date(fecha_hora)", "DATE(fecha_hora::timestamp)")
        sql_adaptado = sql_adaptado.replace("strftime('%H', fecha_hora)", "TO_CHAR(fecha_hora::timestamp, 'HH24')")
        sql_adaptado = sql_adaptado.replace("GROUP_CONCAT(pais, ', ')", "STRING_AGG(pais, ', ')")
    return pd.read_sql(sql_adaptado, conn, params=params)


def ejecutar_sql(cur, sql: str, params=None):
    """Ejecuta una sentencia INSERT/UPDATE/DELETE adaptando placeholders."""
    if not isinstance(cur, sqlite3.Cursor):
        sql = sql.replace("?", "%s")
    if params is not None:
        cur.execute(sql, params)
    else:
        cur.execute(sql)


def insertar_lote(cur, tabla: str, columnas: list, filas: list):
    """Inserción masiva optimizada para PostgreSQL o SQLite."""
    if not filas:
        return
    cols_str = ", ".join(columnas)
    if not isinstance(cur, sqlite3.Cursor):
        sql = f"INSERT INTO {tabla} ({cols_str}) VALUES %s"
        execute_values(cur, sql, filas, page_size=1000)
    else:
        placeholders = ", ".join(["?"] * len(columnas))
        sql = f"INSERT INTO {tabla} ({cols_str}) VALUES ({placeholders})"
        cur.executemany(sql, filas)


def inicializar_esquema_completo(conn):
    """Crea todas las tablas e índices en el esquema 'mer' (PostgreSQL) o SQLite."""
    cur = conn.cursor()
    if es_postgres(conn):
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ESQUEMA_PG};")
        cur.execute(f"SET search_path TO {ESQUEMA_PG}, public;")
        sentencias = [
            """
            CREATE TABLE IF NOT EXISTS dim_archivos_cargados (
                fecha_operacion TEXT,
                codigo_osom TEXT,
                pais TEXT,
                nombre_osom TEXT,
                archivo_origen TEXT,
                timestamp_version TEXT,
                fecha_carga TEXT,
                fecha_publicacion_eor TEXT,
                hash_archivo TEXT,
                PRIMARY KEY (fecha_operacion, codigo_osom)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS fact_precios_exante (
                id SERIAL PRIMARY KEY,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                codigo_nodo TEXT,
                precio_exante_usd DOUBLE PRECISION
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS fact_contratos_tcp (
                id SERIAL PRIMARY KEY,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                pais TEXT,
                codigo_nodo TEXT,
                codigo_agente TEXT,
                punto_medida TEXT,
                codigo_contrato TEXT,
                tipo_contrato TEXT,
                agente_contraparte TEXT,
                tipo_transaccion TEXT,
                mw_requerida DOUBLE PRECISION,
                mw_declarada DOUBLE PRECISION,
                precio_exante DOUBLE PRECISION,
                mw_predespachados DOUBLE PRECISION
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS fact_oportunidad_top (
                id SERIAL PRIMARY KEY,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                pais TEXT,
                codigo_nodo TEXT,
                codigo_agente TEXT,
                punto_medida TEXT,
                tipo_transaccion TEXT,
                mw_requerida DOUBLE PRECISION,
                mw_declarada DOUBLE PRECISION,
                precio_exante DOUBLE PRECISION,
                mw_predespachados DOUBLE PRECISION
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS dim_catalogo_nodos (
                codigo_nodo TEXT PRIMARY KEY,
                pais TEXT,
                nombre_nodo TEXT DEFAULT '',
                subestacion TEXT DEFAULT '',
                nivel_kv TEXT DEFAULT ''
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS dim_catalogo_agentes (
                codigo_agente TEXT PRIMARY KEY,
                pais TEXT,
                nombre_agente TEXT DEFAULT '',
                tipo_agente TEXT DEFAULT ''
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_pex_fecha ON fact_precios_exante(fecha_operacion);",
            "CREATE INDEX IF NOT EXISTS idx_tcp_fecha_pais ON fact_contratos_tcp(fecha_operacion, pais);",
            "CREATE INDEX IF NOT EXISTS idx_top_fecha_pais ON fact_oportunidad_top(fecha_operacion, pais);",
        ]
        for s in sentencias:
            cur.execute(s)
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS dim_archivos_cargados (
                fecha_operacion TEXT,
                codigo_osom TEXT,
                pais TEXT,
                nombre_osom TEXT,
                archivo_origen TEXT,
                timestamp_version TEXT,
                fecha_carga TEXT,
                fecha_publicacion_eor TEXT,
                hash_archivo TEXT,
                PRIMARY KEY (fecha_operacion, codigo_osom)
            );
            CREATE TABLE IF NOT EXISTS fact_precios_exante (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                codigo_nodo TEXT,
                precio_exante_usd REAL
            );
            CREATE TABLE IF NOT EXISTS fact_contratos_tcp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                pais TEXT,
                codigo_nodo TEXT,
                codigo_agente TEXT,
                punto_medida TEXT,
                codigo_contrato TEXT,
                tipo_contrato TEXT,
                agente_contraparte TEXT,
                tipo_transaccion TEXT,
                mw_requerida REAL,
                mw_declarada REAL,
                precio_exante REAL,
                mw_predespachados REAL
            );
            CREATE TABLE IF NOT EXISTS fact_oportunidad_top (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_operacion TEXT,
                fecha_hora TEXT,
                pais TEXT,
                codigo_nodo TEXT,
                codigo_agente TEXT,
                punto_medida TEXT,
                tipo_transaccion TEXT,
                mw_requerida REAL,
                mw_declarada REAL,
                precio_exante REAL,
                mw_predespachados REAL
            );
            CREATE TABLE IF NOT EXISTS dim_catalogo_nodos (
                codigo_nodo TEXT PRIMARY KEY,
                pais TEXT,
                nombre_nodo TEXT DEFAULT '',
                subestacion TEXT DEFAULT '',
                nivel_kv TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dim_catalogo_agentes (
                codigo_agente TEXT PRIMARY KEY,
                pais TEXT,
                nombre_agente TEXT DEFAULT '',
                tipo_agente TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_pex_fecha ON fact_precios_exante(fecha_operacion);
            CREATE INDEX IF NOT EXISTS idx_tcp_fecha_pais ON fact_contratos_tcp(fecha_operacion, pais);
            CREATE INDEX IF NOT EXISTS idx_top_fecha_pais ON fact_oportunidad_top(fecha_operacion, pais);
        """)
    conn.commit()
    cur.close()