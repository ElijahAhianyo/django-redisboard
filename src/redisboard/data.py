import html
import pickle
from abc import ABC
from abc import abstractmethod
from functools import partial
from itertools import chain
from typing import TYPE_CHECKING
from typing import Dict
from typing import List
from typing import Tuple
from typing import Type
from urllib.parse import quote

from django.conf import settings
from django.shortcuts import resolve_url
from django.template.defaultfilters import truncatechars
from django.utils.safestring import mark_safe
from django.utils.translation import gettext
from redis import StrictRedis

from redisboard.structs import KeyInfo
from redisboard.structs import ScanResult
from redisboard.structs import ascii_if_not_none
from redisboard.structs import dash_if_none

if TYPE_CHECKING:
    from redisboard.models import RedisServer

REDISBOARD_SCAN_COUNT: int = getattr(settings, 'REDISBOARD_SCAN_COUNT', 1000)
REDISBOARD_STRING_PAGINATION: int = getattr(settings, 'REDISBOARD_STRING_PAGINATION', 10000)


class BaseDecoder(ABC):
    def __init__(self, server):
        pass

    def key(self, key: bytes):
        return key.decode()

    def bytes(self, key: str, value: bytes):
        return value

    def string(self, key: str, value: bytes, **kwargs):
        return [(len(value), self.bytes(key, value))]

    def hash(self, key: str, hash_value: Dict[bytes, bytes], **kwargs):
        return sorted((self.hash_field(key, k), self.bytes(key, v)) for k, v in hash_value.items())

    def hash_field(self, key: str, field: bytes):
        return self.key(field)

    def list(self, key: str, value: List[bytes], count=int, **kwargs):
        return list(enumerate((self.bytes(key, v) for v in value), start=count))

    def set(self, key: str, value: List[bytes], count: int, **kwargs):
        return list(enumerate(sorted(self.bytes(key, v) for v in value), start=count))

    def zset(self, key: str, value: bytes, **kwargs):
        return sorted((s, self.bytes(key, v)) for v, s in value)

    def unsupported(self, key: str, value, type_: str, **kwargs):
        return [('ERROR', f'Missing decoder; {value}')]

    def __getattr__(self, type_: str):
        return partial(self.unsupported, type_=type_)


class UTF8BackslashReplaceDecoder(BaseDecoder):
    def bytes(self, key: str, value: bytes):
        return value.decode(errors='backslashreplace')

    def key(self, key: bytes):
        return key.decode(errors='backslashreplace')


class PickleDecoder(BaseDecoder):
    def bytes(self, key: str, value: bytes):
        return pickle.loads(value)


class ValueQuery:
    connection: StrictRedis

    def __init__(self, connection: StrictRedis):
        self.connection = connection

    def hash(self, key, *, cursor=0, **kwargs) -> Tuple[int, list]:
        return self.connection.hscan(key, cursor=cursor, count=REDISBOARD_SCAN_COUNT)

    def list(self, key, *, cursor=0, **kwargs) -> Tuple[int, list]:
        end = cursor + REDISBOARD_SCAN_COUNT
        value = self.connection.lrange(key, cursor, end - 1)
        if len(value) < end:
            return 0, value
        else:
            return end, value

    def set(self, key, *, cursor=0, **kwargs) -> Tuple[int, list]:
        return self.connection.sscan(key, cursor=cursor, count=REDISBOARD_SCAN_COUNT)

    def string(self, key, *, cursor=0, count=0, **kwargs) -> Tuple[int, list]:
        start = cursor
        end = count + REDISBOARD_STRING_PAGINATION
        value = self.connection.getrange(key, start, end - 1)
        if value:
            return end, value
        else:
            return 0, value

    def zset(self, key, *, cursor=0, **kwargs) -> Tuple[int, list]:
        return self.connection.zscan(key, cursor=cursor, count=REDISBOARD_SCAN_COUNT)

    def unsupported(self, key, *, cursor=0, type_, **kwargs):
        return cursor, f'Unsupported type {type_!r} for key: {key}'

    def __getattr__(self, type_):
        return partial(self.unsupported, type_=type_)


class LengthQuery:
    connection: StrictRedis

    def __init__(self, connection):
        self.connection = connection

    def string(self, key):
        return self.connection.strlen(key)

    def hash(self, key):
        return self.connection.hlen(key)

    def list(self, key):
        return self.connection.llen(key)

    def set(self, key):
        return self.connection.scard(key)

    def zset(self, key):
        return self.connection.zcount(key, '-inf', '+inf')

    def unsupported(self, key):
        return self.connection.echo('-1')

    def __getattr__(self, type_):
        return self.unsupported


class BaseDisplay(ABC):
    decoder: BaseDecoder
    value_query_class: Type[ValueQuery]
    length_query_class: Type[LengthQuery]
    server: 'RedisServer'

    def __init__(
        self,
        decoder_class: Type[BaseDecoder],
        value_query_class: Type[ValueQuery],
        length_query_class: Type[LengthQuery],
        server: 'RedisServer',
    ):
        self.decoder = decoder_class(server)
        self.value_query_class = value_query_class
        self.length_query_class = length_query_class
        self.server = server

    @abstractmethod
    def slowlog(self):
        pass

    @abstractmethod
    def details(self):
        pass

    @abstractmethod
    def cpu(self):
        pass

    def keys(self, db, keys):
        conn = self.server.connection
        conn.select(db)

        if self.server.has_frequency:
            usage_field, usage_command = 'frequency', 'FREQ'
        else:
            usage_field, usage_command = 'idletime', 'IDLETIME'

        with conn.pipeline() as pipe:
            for key in keys:
                pipe.type(key)
                pipe.object('ENCODING', key)
            result = iter(map(ascii_if_not_none, pipe.execute()))
            values = list(zip(keys, result, result))

        with conn.pipeline() as pipe:
            query = self.length_query_class(pipe)
            for key, type_, *_ in values:
                pipe.ttl(key)
                pipe.object(usage_command, key)
                getattr(query, type_)(key)
            result = iter(pipe.execute())

        fields = ['name', 'type', 'encoding', 'ttl', usage_field, 'length']
        values = list(map(list, map(chain.from_iterable, zip(values, zip(result, result, result)))))

        return [KeyInfo(**dict(zip(fields, v))) for v in values]

    def scan(self, db, cursor=0, match=None, type=None) -> ScanResult:
        conn = self.server.connection
        conn.select(db)

        total = conn.dbsize()
        cursor, keys = conn.scan(cursor=cursor, count=REDISBOARD_SCAN_COUNT, match=match, _type=type)
        return ScanResult(cursor, len(keys), total, self.keys(db, keys))

    def value(self, db, key, **kwargs):
        conn = self.server.connection
        conn.select(db)
        type_ = conn.type(key).decode()
        total = getattr(self.length_query_class(conn), type_)(key)
        cursor, value = getattr(self.value_query_class(conn), type_)(key, **kwargs)
        decoded_value = getattr(self.decoder, type_)(key, value, **kwargs)
        return ScanResult(cursor, len(value), total, decoded_value)


class TabularDisplay(BaseDisplay):
    def slowlog(self):
        commands = []
        slowlog = self.server.stats.slowlog
        for log in slowlog:
            command = log['command']
            if isinstance(command, bytes):
                command = command.decode(errors='backslashreplace')

            if len(command) > 255:
                command = f'{command:252}...'

            commands.append((log['duration'], command))
        commands.sort(reverse=True)
        if commands:
            output = ''.join(f'<tr><th>{duration / 1000.0:.1f}ms</th><td>{command}</td></tr>' for duration, command in commands)
            return mark_safe(f'<table><tr><th colspan="2">Total: {len(slowlog)} items</th></tr>{output}</table>')
        else:
            return 'n/a'

    def details(self):
        stats = self.server.stats
        details = stats.details
        output = [f'<tr><th>{k.replace("_", " ")}</th><td>{v}</td></tr>' for k, v in details.items()]
        for db, details in stats.databases.items():
            keys = details.keys()
            output.append(f'<tr><td colspan="2"><table><tr><th>db{db}</th>')
            output.extend(f'<th>{k}</th>' for k in keys)
            output.append('</tr><tr><td></td>')
            output.extend(f'<td>{details[k]}</td>' for k in keys)
            output.append('</tr></table></td></tr>')
        if output:
            return mark_safe(f'<table>{"".join(output)}</table>')
        return 'n/a'

    def cpu(self):
        stats = self.server.stats
        if stats.status != 'UP':
            return 'n/a'

        data = (
            'used_cpu_sys',
            'used_cpu_sys_children',
            'used_cpu_user',
            'used_cpu_user_children',
        )
        data = dict((k, stats.info[k]) for k in data)
        total_cpu = sum(data.values())
        uptime = stats.info['uptime_in_seconds']
        data['cpu_utilization'] = f'{total_cpu / uptime if uptime else 0:.3f}%%'

        data = sorted(data.items())

        output = []
        for k, v in data:
            k = k.replace('_', ' ')
            output.append(f'<tr><th>{k}</th><td>{v}</td></tr>')

        if output:
            return mark_safe(f'<table>{"".join(output)}</table>')
        return 'n/a'

    def keys(self, db, *args):
        keys = super().keys(db, *args)
        has_frequency = self.server.has_frequency
        output = [
            '<table><thead><tr><th rowspan="2">',
            gettext('Keys'),
            f' ({len(keys)})</th><th colspan="7">',
            gettext('Details'),
            '</th></tr><tr><th>',
            gettext('Type'),
            '</th><th>',
            gettext('TTL'),
            '</th><th>',
            gettext('Encoding'),
            '</th><th>',
            gettext('Length'),
            '</th><th>',
            gettext('Frequency') if self.server.has_frequency else gettext('Idletime'),
            '</th></tr></thead>',
        ]

        output.extend(
            f'<tr><td><a href="'
            f'{resolve_url("admin:redisboard_redisserver_inspect", server_id=self.server.id, db=db, key=quote(key.name))}">'
            f'{html.escape(truncatechars(self.decoder.key(key.name), 200))}</a></td>'
            f'<td>{key.type}</td>'
            f'<td>{dash_if_none(key.ttl)}</td>'
            f'<td>{dash_if_none(key.encoding)}</td>'
            f'<td>{dash_if_none(key.length)}</td>'
            f'<td>{key.frequency if has_frequency else key.idletime}</td>'
            '</tr>'
            for key in keys
        )
        output.append('</table>')
        return mark_safe(''.join(output))

    def value(self, db, key, **kwargs):
        cursor, count, total, data = super().value(db, key, **kwargs)
        return ScanResult(
            cursor,
            count,
            total,
            mark_safe(
                ''.join(
                    chain(
                        ('<table>',),
                        (f'<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}' for k, v in data),
                        ('</table>',),
                    )
                )
            ),
        )