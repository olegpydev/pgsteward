"""Логирование в stderr.

Настраивается логгер `pgsteward`, а не root. Библиотека без собственного
хендлера пропагирует записи в root, поэтому хендлер на нём печатал бы
нашим форматом и чужое: fastmcp тянет pydocket, и его INFO шёл бы
вперемешку с нашим. На `DEBUG` своё сообщение пришлось бы выискивать
среди внутренностей драйвера.

Чужие предупреждения при этом видны: на root хендлеров не остаётся,
Python не находит ни одного и включает `logging.lastResort`, который
печатает WARNING и выше в stderr. Отсекается шум, а не проблемы.

`configure_logging` вызывается из `main()`, а не на импорте: настройка
логгера — решение приложения, и импорт модуля не должен её навязывать.
"""

import logging
import sys

logger = logging.getLogger('pgsteward')

_FORMAT = '%(levelname)s: [%(asctime)s,%(msecs)03.0f] "%(name)s" %(message)s'
_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    # Присваивание, а не `addHandler`: второй вызов иначе добавил бы
    # ещё один хендлер и задвоил каждую строку.
    logger.handlers = [handler]
    logger.setLevel(level)
    # Иначе записи уходят дальше в root, и хендлер, добавленный туда
    # кем-то ещё, напечатает их второй раз.
    logger.propagate = False
