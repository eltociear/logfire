from collections import ChainMap
from typing import Any, Mapping

from inline_snapshot import snapshot

from logfire._internal.formatter import chunks_formatter, logfire_format
from logfire._internal.scrubbing import Scrubber


def chunks(format_string: str, kwargs: Mapping[str, Any]):
    return chunks_formatter.chunks(format_string, kwargs, scrubber=Scrubber([]))


def test_simple_render():
    v = chunks('foo {bar}', {'bar': 'span'})
    assert v == [{'v': 'foo ', 't': 'lit'}, {'v': 'span', 't': 'arg'}]


def test_spec():
    v = chunks('foo {bar:0.2f}', ChainMap({}, {'bar': 123.456}))
    assert v == snapshot([{'v': 'foo ', 't': 'lit'}, {'v': '123.46', 't': 'arg', 'spec': '0.2f'}])


def test_insert_name():
    v = chunks('foo {bar=}', {'bar': 42})
    assert v == snapshot([{'v': 'foo bar=', 't': 'lit'}, {'v': '42', 't': 'arg'}])


def test_insert_name_spec():
    v = chunks('foo {bar=:d}', {'bar': 42})
    assert v == snapshot([{'v': 'foo bar=', 't': 'lit'}, {'v': '42', 't': 'arg', 'spec': 'd'}])


def test_first():
    v = chunks('{bar}', {'bar': 42})
    assert v == snapshot([{'v': '42', 't': 'arg'}])


def test_insert_first():
    v = chunks('{bar=}', {'bar': 42})
    assert v == snapshot([{'v': 'bar=', 't': 'lit'}, {'v': '42', 't': 'arg'}])


def test_three():
    v = chunks('{foo} {bar} {spam}', ChainMap({'foo': 1, 'bar': 2}, {'spam': '3'}))
    assert v == snapshot(
        [
            {'v': '1', 't': 'arg'},
            {'v': ' ', 't': 'lit'},
            {'v': '2', 't': 'arg'},
            {'v': ' ', 't': 'lit'},
            {'v': '3', 't': 'arg'},
        ]
    )


def test_dict():
    v = chunks('{foo[bar]}', {'foo': {'bar': 42}})
    assert v == snapshot([{'v': '42', 't': 'arg'}])


def test_truncate():
    message = logfire_format(
        '1 {a} 2 {b} 3',
        dict(
            a='a' * 1000,
            b='b' * 1000,
        ),
        scrubber=Scrubber([]),
    )
    assert message == snapshot(
        '1 '
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        '...'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        ' 2 '
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
        '...'
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
        ' 3'
    )
    assert len(message) == snapshot(261)
