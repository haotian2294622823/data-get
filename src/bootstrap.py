import os
from src.catalog import load_config
from src.data_access.mock import MockFactory
from src.models import DataSourceFactory
from src.services.query_service import QueryService


def build_service(mode: str, factory: DataSourceFactory | None = None) -> QueryService:
    catalog, limits, version = load_config(mode)
    if factory is None:
        if mode == "mock":
            factory = MockFactory()
        else:
            from src.data_access.postgres import PostgresFactory

            factory = PostgresFactory(os.getenv("DATABASE_URL", ""))
    return QueryService(mode, factory, catalog, limits, version)
