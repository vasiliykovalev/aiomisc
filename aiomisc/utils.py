import asyncio
import itertools
import logging.handlers
import socket
from functools import wraps
from multiprocessing import cpu_count
from types import CoroutineType
from typing import Any, Iterable, Tuple

try:
    from typing import Coroutine
except ImportError:
    Coroutine = CoroutineType


try:
    import uvloop
except ImportError:
    uvloop = None


from .thread_pool import ThreadPoolExecutor


log = logging.getLogger(__name__)


def chunk_list(iterable: Iterable[Any], size: int):
    """
    Split list or generator by chunks with fixed maximum size.
    """

    iterable = iter(iterable)

    item = list(itertools.islice(iterable, size))
    while item:
        yield item
        item = list(itertools.islice(iterable, size))


OptionsType = Iterable[Tuple[int, int, int]]


def bind_socket(*args, address: str, port: int, options: OptionsType = (),
                reuse_addr: bool = True, reuse_port: bool = False,
                proto_name: str = 'tcp'):
    """

    :param args: which will be passed to stdlib's socket constructor (optional)
    :param address: bind address
    :param port: bind port
    :param options: Tuple of pairs which contain socket option
                    to set and the option value.
    :param reuse_addr: set socket.SO_REUSEADDR
    :param reuse_port: set socket.SO_REUSEPORT
    :param proto_name: protocol name which will be logged after binding
    :return: socket.socket
    """

    if not args:
        if ':' in address:
            args = (socket.AF_INET6, socket.SOCK_STREAM)
        else:
            args = (socket.AF_INET, socket.SOCK_STREAM)

    sock = socket.socket(*args)
    sock.setblocking(False)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, int(reuse_addr))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, int(reuse_port))

    for level, option, value in options:
        sock.setsockopt(level, option, value)

    sock.bind((address, port))
    sock_addr = sock.getsockname()[:2]

    if sock.family == socket.AF_INET6:
        log.info('Listening %s://[%s]:%s', proto_name, *sock_addr)
    else:
        log.info('Listening %s://%s:%s', proto_name, *sock_addr)

    return sock


def new_event_loop(pool_size=None) -> asyncio.AbstractEventLoop:
    if uvloop:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    pool_size = pool_size or cpu_count()

    try:
        asyncio.get_event_loop().close()
    except RuntimeError:
        pass  # event loop is not created yet

    loop = asyncio.new_event_loop()
    thread_pool = ThreadPoolExecutor(pool_size, loop=loop)

    loop.set_default_executor(thread_pool)

    asyncio.set_event_loop(loop)

    return loop


def shield(func):
    """
    Simple and useful decorator for wrap the coroutine to `asyncio.shield`.

    >>> @shield
    ... async def non_cancelable_func():
    ...     await asyncio.sleep(1)

    """

    async def awaiter(future):
        return await future

    @wraps(func)
    def wrap(*args, **kwargs):
        return wraps(func)(awaiter)(asyncio.shield(func(*args, **kwargs)))

    return wrap


class SelectResult(list):
    def __init__(self, *args, **kwargs):
        self.result_idx = None
        self.is_exception = None
        super().__init__(*args, **kwargs)

    def set_result(self, idx, value, is_exception):
        if self.result_idx is not None:
            raise RuntimeError("Result already set")

        self[idx] = value
        self.result_idx = idx
        self.is_exception = is_exception

    def result(self):
        res = self[self.result_idx]

        if self.is_exception:
            raise res

        return res


async def select(*awaitables, cancel=True, loop=None) -> SelectResult:
    loop = loop or asyncio.get_event_loop()
    result = SelectResult([None] * len(awaitables))

    async def cancel_others(pending):
        if not pending:
            return

        for coro in pending:
            coro.cancel()

        await asyncio.gather(*pending, return_exceptions=True, loop=loop)

    async def waiter(idx, awaitable):
        nonlocal result
        try:
            ret = await awaitable
        except Exception as e:
            result.set_result(idx, e, True)
        else:
            result.set_result(idx, ret, False)

    _, pending = await asyncio.wait(
        [waiter(i, c) for i, c in enumerate(awaitables)],
        loop=loop, return_when=asyncio.FIRST_COMPLETED,
    )

    if cancel:
        await loop.create_task(cancel_others(pending))

    if result.is_exception:
        result.result()

    return result
