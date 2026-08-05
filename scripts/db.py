"""Database connection helpers for the Custimoo bronze PostgreSQL replica."""

import os

import psycopg2


def connect():
    """Connect to the Custimoo PostgreSQL bronze dataset via the local SSH tunnel."""
    return psycopg2.connect(
        host=os.environ.get("CUSTIMOO_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("CUSTIMOO_DB_PORT", "5433")),
        dbname=os.environ.get("CUSTIMOO_DB_NAME", "custimoo-prod"),
        user=os.environ.get("CUSTIMOO_DB_USER", "lakr@custimoo.com"),
        password=os.environ.get("CUSTIMOO_DB_PASSWORD", ""),
        sslmode=os.environ.get("CUSTIMOO_DB_SSLMODE", "require"),
        connect_timeout=int(os.environ.get("CUSTIMOO_DB_CONNECT_TIMEOUT", "10")),
        options="-c search_path=bronze,public",
    )


def qty_expr(alias="o"):
    """Postgres expression for order-level total quantity."""
    return f"COALESCE(NULLIF(({alias}.price_info->>'total_quantity'), '')::numeric, 0)::int"
