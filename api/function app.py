"""
Generische Admin-API für SecuritiesDBtwo.
Funktioniert fuer beliebige Tabellen (erkennt Spalten & Primaerschluessel
automatisch ueber INFORMATION_SCHEMA), damit nicht pro Tabelle eigener
Code noetig ist.

Passwortschutz: einfacher Shared-Secret-Header 'x-admin-password',
verglichen mit der App-Setting ADMIN_PASSWORD. Reicht fuer Single-User-Zugriff,
ist aber kein vollwertiges Auth-System - Function-URL nicht weitergeben.
"""

import json
import logging
import os
import re
import time
import pyodbc
import azure.functions as func
from datetime import datetime, date

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

VALID_TABLE_NAME = re.compile(r'^[A-Za-z0-9_]+$')


def get_connection():
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={os.environ['SQL_SERVER']};"
        f"DATABASE={os.environ['SQL_DATABASE']};"
        f"UID={os.environ['SQL_USER']};"
        f"PWD={os.environ['SQL_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=90;"
    )
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            return pyodbc.connect(conn_str)
        except pyodbc.OperationalError as e:
            if attempt == max_attempts:
                raise
            logging.warning(
                f"Verbindung fehlgeschlagen (Versuch {attempt}/{max_attempts}), "
                f"DB evtl. noch am Aufwachen (Serverless Auto-Pause). Warte 20s... ({e})"
            )
            time.sleep(20)


def check_auth(req: func.HttpRequest) -> bool:
    supplied = req.headers.get('x-admin-password', '')
    expected = os.environ.get('ADMIN_PASSWORD', '')
    return bool(expected) and supplied == expected


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def json_response(data, status_code=200):
    return func.HttpResponse(
        json.dumps(data, default=json_default),
        status_code=status_code,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def unauthorized():
    return json_response({"error": "Unauthorized"}, status_code=401)


REFERENCE_LABEL_COLUMN = {
    "security_master": "Name",
    "security_parameter_types": "ParameterName",
}


def get_foreign_keys(conn, table):
    """Liefert {FK-Spalte: (Ref-Tabelle, Ref-Spalte)} fuer eine Tabelle."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT fkc.COLUMN_NAME AS FKColumn, pkt.TABLE_NAME AS RefTable, pkc.COLUMN_NAME AS RefColumn
        FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fkc
          ON rc.CONSTRAINT_NAME = fkc.CONSTRAINT_NAME AND rc.CONSTRAINT_SCHEMA = fkc.CONSTRAINT_SCHEMA
        JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS pkt
          ON rc.UNIQUE_CONSTRAINT_NAME = pkt.CONSTRAINT_NAME AND rc.UNIQUE_CONSTRAINT_SCHEMA = pkt.CONSTRAINT_SCHEMA
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pkc
          ON pkt.CONSTRAINT_NAME = pkc.CONSTRAINT_NAME
        WHERE fkc.TABLE_NAME = ?
        """,
        table,
    )
    return {r.FKColumn: (r.RefTable, r.RefColumn) for r in cursor.fetchall()}


def get_lookup_options(conn, ref_table, ref_column, label_column):
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT TOP 2000 [{ref_column}] AS val, [{label_column}] AS label "
        f"FROM [{ref_table}] ORDER BY [{label_column}]"
    )
    return [{"value": r.val, "label": r.label} for r in cursor.fetchall()]


def is_valid_table(conn, table):
    if not VALID_TABLE_NAME.match(table or ""):
        return False
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?", table)
    return cursor.fetchone() is not None


def get_primary_key(conn, table):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ku.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
          ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME AND tc.TABLE_NAME = ku.TABLE_NAME
        WHERE tc.TABLE_NAME = ? AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        """,
        table,
    )
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute(
        "SELECT TOP 1 COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        table,
    )
    fallback = cursor.fetchone()
    return fallback[0] if fallback else None


@app.route(route="tables", methods=["GET"])
def list_tables(req: func.HttpRequest) -> func.HttpResponse:
    if not check_auth(req):
        return unauthorized()
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
        tables = [r[0] for r in cursor.fetchall()]
        conn.close()
        return json_response(tables)
    except Exception as e:
        logging.error(str(e))
        return json_response({"error": str(e)}, status_code=500)


@app.route(route="tables/{table}", methods=["GET"])
def get_table(req: func.HttpRequest) -> func.HttpResponse:
    if not check_auth(req):
        return unauthorized()
    table = req.route_params.get("table")
    search = req.params.get("search", "").strip()
    try:
        limit = min(int(req.params.get("limit", 100)), 500)
    except ValueError:
        limit = 100
    try:
        offset = max(int(req.params.get("offset", 0)), 0)
    except ValueError:
        offset = 0

    try:
        conn = get_connection()
        if not is_valid_table(conn, table):
            return json_response({"error": "Invalid table"}, status_code=400)

        cursor = conn.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            table,
        )
        columns = [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in cursor.fetchall()]
        col_names = [c["name"] for c in columns]

        pk = get_primary_key(conn, table)

        fks = get_foreign_keys(conn, table)
        lookups = {}
        for col in columns:
            ref = fks.get(col["name"])
            if ref:
                ref_table, ref_column = ref
                label_col = REFERENCE_LABEL_COLUMN.get(ref_table)
                if label_col:
                    col["linkedTable"] = ref_table
                    col["linkedLabelColumn"] = label_col
                    lookups[col["name"]] = get_lookup_options(conn, ref_table, ref_column, label_col)

        if search:
            where_clause = " OR ".join(f"CAST([{c}] AS NVARCHAR(MAX)) LIKE ?" for c in col_names)
            search_params = [f"%{search}%"] * len(col_names)
        else:
            where_clause = "1=1"
            search_params = []

        cursor.execute(f"SELECT COUNT(*) FROM [{table}] WHERE {where_clause}", search_params)
        total = cursor.fetchone()[0]

        cursor.execute(
            f"SELECT * FROM [{table}] WHERE {where_clause} "
            f"ORDER BY [{pk}] DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
            search_params + [offset, limit],
        )
        result_col_names = [c[0] for c in cursor.description]
        rows = [dict(zip(result_col_names, row)) for row in cursor.fetchall()]

        conn.close()
        return json_response({
            "columns": columns,
            "primaryKey": pk,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "lookups": lookups,
        })
    except Exception as e:
        logging.error(str(e))
        return json_response({"error": str(e)}, status_code=500)


@app.route(route="tables/{table}", methods=["POST"])
def insert_row(req: func.HttpRequest) -> func.HttpResponse:
    if not check_auth(req):
        return unauthorized()
    table = req.route_params.get("table")
    try:
        body = req.get_json()
        conn = get_connection()
        if not is_valid_table(conn, table):
            return json_response({"error": "Invalid table"}, status_code=400)

        cols = list(body.keys())
        if not cols:
            return json_response({"error": "No columns provided"}, status_code=400)
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f"[{c}]" for c in cols)
        values = [body[c] for c in cols]

        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO [{table}] ({col_list}) VALUES ({placeholders})", values)
        conn.commit()
        conn.close()
        return json_response({"success": True})
    except Exception as e:
        logging.error(str(e))
        return json_response({"error": str(e)}, status_code=500)


@app.route(route="tables/{table}", methods=["PUT"])
def update_row(req: func.HttpRequest) -> func.HttpResponse:
    if not check_auth(req):
        return unauthorized()
    table = req.route_params.get("table")
    try:
        body = req.get_json()
        pk_column = body["pkColumn"]
        pk_value = body["pkValue"]
        updates = dict(body["updates"])

        conn = get_connection()
        if not is_valid_table(conn, table):
            return json_response({"error": "Invalid table"}, status_code=400)

        cursor = conn.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?", table
        )
        all_columns = {r[0] for r in cursor.fetchall()}

        # Modified_at/Created_at nie vom Client uebernehmen - immer serverseitig setzen bzw. unangetastet lassen
        updates.pop("Modified_at", None)
        updates.pop("Created_at", None)

        set_parts = [f"[{c}] = ?" for c in updates.keys()]
        values = list(updates.values())

        if "Modified_at" in all_columns:
            set_parts.append("[Modified_at] = SYSUTCDATETIME()")

        if not set_parts:
            return json_response({"error": "No updates provided"}, status_code=400)

        set_clause = ", ".join(set_parts)
        values.append(pk_value)

        cursor.execute(f"UPDATE [{table}] SET {set_clause} WHERE [{pk_column}] = ?", values)
        conn.commit()
        conn.close()
        return json_response({"success": True})
    except Exception as e:
        logging.error(str(e))
        return json_response({"error": str(e)}, status_code=500)


@app.route(route="tables/{table}", methods=["DELETE"])
def delete_row(req: func.HttpRequest) -> func.HttpResponse:
    if not check_auth(req):
        return unauthorized()
    table = req.route_params.get("table")
    try:
        body = req.get_json()
        pk_column = body["pkColumn"]
        pk_value = body["pkValue"]

        conn = get_connection()
        if not is_valid_table(conn, table):
            return json_response({"error": "Invalid table"}, status_code=400)

        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM [{table}] WHERE [{pk_column}] = ?", (pk_value,))
        conn.commit()
        conn.close()
        return json_response({"success": True})
    except Exception as e:
        logging.error(str(e))
        return json_response({"error": str(e)}, status_code=500)
