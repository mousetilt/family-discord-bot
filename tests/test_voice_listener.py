import asyncio
import threading
import unittest

from cogs.jarvis import Listener


class Conn:
    def __init__(self):
        self.cbs = []

    def add_socket_listener(self, cb):
        self.cbs.append(cb)

    def remove_socket_listener(self, cb):
        self.cbs.remove(cb)


class VC:
    def __init__(self, conn):
        self._connection = conn


class SocketListener(unittest.IsolatedAsyncioTestCase):
    """Issue #5: звук берём штатным слушателем discord.py, а не loop.sock_recv (гонка по номеру сокета)."""

    def make(self):
        li = object.__new__(Listener)
        li.inbox, li.sub = asyncio.Queue(maxsize=3), None
        return li

    async def test_packets_from_thread_and_resubscribe(self):
        li, a, b = self.make(), Conn(), Conn()
        self.assertTrue(li.subscribe(VC(a)))
        self.assertTrue(li.subscribe(VC(a)))  # повторно на то же соединение — не дублируем
        self.assertEqual(len(a.cbs), 1)
        t = threading.Thread(target=lambda: [a.cbs[0](b"p%d" % i) for i in range(5)])
        t.start(); t.join()
        await asyncio.sleep(0.05)
        self.assertEqual(li.inbox.qsize(), 3)  # лишнее выкинуто, а не копится
        self.assertEqual(await li.inbox.get(), b"p0")
        li.subscribe(VC(b))  # переподключились — слушатель переехал
        self.assertEqual((len(a.cbs), len(b.cbs)), (0, 1))
        li.unsubscribe()
        self.assertEqual(b.cbs, [])

    async def test_no_connection(self):
        self.assertFalse(self.make().subscribe(object()))
