import json
from typing import no_type_check, Type, Any
from pydantic.v1 import BaseModel, ValidationError, StrBytes, Protocol

from ogmios.logger import logger


class BaseModelNoException(BaseModel):
    def __init__(__pydantic_self__, **data: Any) -> None:
        try:
            super(BaseModelNoException, __pydantic_self__).__init__(**data)
        except ValidationError as pve:
            logger.warning(
                f'pydantic warning:  __init__ failed to validate:\n {json.dumps(data, indent=4)}\n'
            )
            logger.warning(f'This is the original exception:\n{pve.json()}')

    @no_type_check
    def __setattr__(self, name, value):
        try:
            return super(BaseModelNoException, self).__setattr__
        except ValidationError as pve:
            logger.warning(
                f'pydantic warning:  __setattr__ failed to validate:\n {json.dumps({name: value}, indent=4)}'
            )
            logger.warning(f'This is the original exception:\n{pve.json()}')
            return None

    @classmethod
    def parse_obj(cls: Type['Model'], obj: Any) -> 'Model':
        try:
            return super(BaseModelNoException, cls).parse_obj(obj)
        except ValidationError as pve:
            logger.warning(
                f'pydantic warning:  parse_obj failed to validate:\n {json.dumps(obj, indent=4)}'
            )
            logger.warning(f'This is the original exception:\n{pve.json()}')
            return None

    @classmethod
    def parse_raw(
        cls: Type['Model'],
        b: StrBytes,
        *,
        content_type: str = None,
        encoding: str = 'utf8',
        proto: Protocol = None,
        allow_pickle: bool = False,
    ) -> 'Model':
        try:
            return super(BaseModelNoException, cls).parse_raw(
                b=b,
                content_type=content_type,
                encoding=encoding,
                proto=proto,
                allow_pickle=allow_pickle,
            )
        except ValidationError as pve:
            logger.warning(f'pydantic warning:  parse_raw failed to validate:\n {b}')
            logger.warning(f'This is the original exception:\n{pve.json()}')
            return None
