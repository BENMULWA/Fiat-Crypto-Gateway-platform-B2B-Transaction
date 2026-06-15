from warnings import warn

# Re-export types from cbor2 for compatibility
from cbor2 import CBORDecodeEOF as CBORDecodeEOF
from cbor2 import CBORDecodeError as CBORDecodeError
from cbor2 import CBORDecodeValueError as CBORDecodeValueError
from cbor2 import CBOREncodeError as CBOREncodeError
from cbor2 import CBOREncodeTypeError as CBOREncodeTypeError
from cbor2 import CBOREncodeValueError as CBOREncodeValueError
from cbor2 import CBORError as CBORError
from cbor2 import CBORSimpleValue as CBORSimpleValue
from cbor2 import CBORTag as CBORTag
from cbor2 import FrozenDict as FrozenDict
from cbor2 import undefined as undefined

warn(
    "The cbor2pure.types module has been deprecated. "
    "Instead import everything directly from cbor2pure."
)
