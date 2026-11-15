from pathlib import Path
import sys

repository_root = Path(__file__).resolve().parents[1]
if str(repository_root) not in sys.path:
    sys.path.insert(0, str(repository_root))

from apps.api.config import ConfigurationError, load_settings


def main() -> int:
    try:
        database_url = load_settings().require_postgres_url()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:
        print(
            "The PostgreSQL driver is missing. Install the project dependencies "
            "from requirements.txt.",
            file=sys.stderr,
        )
        return 2

    connection = None
    close_failed = False
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
        if result != (1,):
            print("PostgreSQL connectivity check failed: SELECT 1 returned an unexpected result.")
            return 1
    except psycopg.Error as error:
        print(
            "PostgreSQL connectivity check failed "
            f"({type(error).__name__}); verify DATABASE_URL and server availability.",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except psycopg.Error as error:
                print(
                    "PostgreSQL connection close failed "
                    f"({type(error).__name__}).",
                    file=sys.stderr,
                )
                close_failed = True

    if close_failed:
        return 1
    print("PostgreSQL connectivity successful (SELECT 1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
