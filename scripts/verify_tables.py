import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from apps.api.config import load_settings
from sqlalchemy import create_engine, text


def main():
    db_url = load_settings().require_postgres_url()
    engine = create_engine(db_url)
    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT table_name "
                "FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "ORDER BY table_name"
            )
        )
        tables = [row[0] for row in res.fetchall()]
        print("Found tables in public schema:")
        for t in tables:
            print(f"  - {t}")

        print("\nChecking columns and row counts for each MVP table:")
        for t in [
            "suppliers",
            "purchase_orders",
            "purchase_order_items",
            "documents",
            "extracted_document_data",
            "exceptions",
            "exception_history",
        ]:
            if t in tables:
                cols = conn.execute(
                    text(
                        "SELECT column_name, data_type "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :tname "
                        "ORDER BY ordinal_position"
                    ),
                    {"tname": t},
                ).fetchall()
                count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"\nTable '{t}' (row count: {count}):")
                for c in cols:
                    print(f"    {c[0]}: {c[1]}")
            else:
                print(f"\nTable '{t}': MISSING!")


if __name__ == "__main__":
    main()
