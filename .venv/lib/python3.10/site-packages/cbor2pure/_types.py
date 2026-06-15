"""Internal types for cbor2pure. Public types are imported from cbor2."""


class BreakMarkerType:
    """Internal sentinel for indefinite-length decoding."""

    __slots__ = ()

    _instance = None

    def __new__(cls: type["BreakMarkerType"]) -> "BreakMarkerType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "break_marker"

    def __bool__(self) -> bool:
        return True


#: Internal sentinel value for indefinite-length decoding
break_marker = BreakMarkerType()
