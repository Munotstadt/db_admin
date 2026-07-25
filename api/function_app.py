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
    return pyodbc.connect(conn_str)


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

        pk = get_primary_key(conn, table)

        cursor.execute(f"SELECT TOP 500 * FROM [{table}] ORDER BY [{pk}] DESC")
        col_names = [c[0] for c in cursor.description]
        rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]

        conn.close()
        return json_response({"columns": columns, "primaryKey": pk, "rows": rows})
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
        updates = body["updates"]
        if not updates:
            return json_response({"error": "No updates provided"}, status_code=400)

        conn = get_connection()
        if not is_valid_table(conn, table):
            return json_response({"error": "Invalid table"}, status_code=400)

        set_clause = ", ".join(f"[{c}] = ?" for c in updates.keys())
        values = list(updates.values()) + [pk_value]

        cursor = conn.cursor()
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
