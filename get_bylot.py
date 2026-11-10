"""Archive the six source tables for a single LOT from the command line."""

import argparse

import psycopg

from query_service import Filters, archive_query, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lot_no", nargs="?")
    args = parser.parse_args()
    load_settings()
    try:
        archive, count = archive_query(Filters(lot_no=args.lot_no or input("LOT: ")))
    except (ValueError, OSError, psycopg.Error) as error:
        parser.exit(1, f"Read failed ({type(error).__name__}). Check .env and database access.\n")
    print(f"Archived {count} LOT(s): {archive}")


if __name__ == "__main__":
    main()
