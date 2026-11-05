class AppError(Exception):
    """可向用户展示的业务错误。"""


class DataSourceNotConfigured(AppError):
    pass


class DataSourceError(AppError):
    pass


class DataContractError(AppError):
    pass


class LimitExceeded(AppError):
    pass


class LotNotFound(AppError):
    pass
