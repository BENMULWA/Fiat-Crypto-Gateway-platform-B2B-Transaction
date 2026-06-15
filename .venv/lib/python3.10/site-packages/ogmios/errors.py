class InvalidMethodError(Exception):
    """Raised when Ogmios response contains an unexpected method name"""

    pass


class InvalidResponseError(Exception):
    """Raised when Ogmios response contains unexpected content"""

    pass


class InvalidOgmiosParameter(Exception):
    """Raised when missing or invalid parameters are passed to an Ogmios object"""

    pass


class ResponseError(Exception):
    """Raised when an Ogmios request contains an error message"""

    pass
