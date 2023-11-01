from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from inspect import Parameter as SignatureParameter, signature as inspect_signature
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Iterator,
    Mapping,
    Sequence,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

import opentelemetry.context as context_api
import opentelemetry.trace as trace_api
import rich.traceback
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Tracer
from opentelemetry.util import types as otel_types
from typing_extensions import LiteralString

from logfire._config import GLOBAL_CONFIG, LogfireConfig
from logfire.version import VERSION

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = None
from typing_extensions import ParamSpec

from logfire._formatter import logfire_format

from ._constants import (
    ATTRIBUTES_LOG_LEVEL_KEY,
    ATTRIBUTES_MESSAGE_KEY,
    ATTRIBUTES_MESSAGE_TEMPLATE_KEY,
    ATTRIBUTES_SPAN_TYPE_KEY,
    ATTRIBUTES_TAGS_KEY,
    ATTRIBUTES_VALIDATION_ERROR_KEY,
    NON_SCALAR_VAR_SUFFIX,
    NULL_ARGS_KEY,
    OTLP_LARGE_INT_SUFFIX,
    OTLP_MAX_INT_SIZE,
    LevelName,
)
from ._flatten import Flatten
from ._json_encoder import json_dumps_traceback, logfire_json_dumps
from ._tracer import ProxyTracerProvider

_CWD = Path('.').resolve()


class Logfire:
    """The main logfire class."""

    def __init__(self, tags: Sequence[str] = (), config: LogfireConfig = GLOBAL_CONFIG) -> None:
        self._tags = list(tags)
        self._config = config
        self.__tracer_provider: ProxyTracerProvider | None = None
        self._logs_tracer: Tracer | None = None
        self._spans_tracer: Tracer | None = None

    def tags(self, *tags: str) -> Logfire:
        """A new Logfire instance with the given tags applied.

        ```py
        import logfire

        with logfire.tags('tag1'):
            logfire.info('new log 1')
        ```

        Args:
            tags: The tags to bind.

        Returns:
            A new Logfire instance with the tags applied.
        """
        return Logfire(self._tags + list(tags), self._config)

    def _get_tracer_provider(self) -> ProxyTracerProvider:
        if self.__tracer_provider is None:
            self.__tracer_provider = self._config.get_tracer_provider()
        return self.__tracer_provider

    def _span(
        self,
        msg_template: LiteralString,
        attributes: dict[str, Any],
        *,
        span_name: str | None = None,
        stacklevel: int = 3,
        decorator: bool = False,
    ) -> ContextManager[LogfireSpan]:
        stack_info = _get_caller_stack_info(stacklevel=stacklevel)

        merged_attributes = {**stack_info, **ATTRIBUTES.get(), **attributes}
        merged_attributes[ATTRIBUTES_MESSAGE_TEMPLATE_KEY] = msg_template

        tags = _merge_tags_into_attributes(merged_attributes, self._tags)

        span_name_: str
        if span_name is not None:
            span_name_ = span_name
        else:
            span_name_ = msg_template
        format_kwargs = {'span_name': span_name_, **merged_attributes}
        log_message = logfire_format(msg_template, format_kwargs, fallback='...', stacklevel=stacklevel)

        merged_attributes[ATTRIBUTES_MESSAGE_KEY] = log_message

        if self._spans_tracer is None:
            self._spans_tracer = self._get_tracer_provider().get_tracer(
                'logfire',  # the name here is really not important, logfire itself doesn't use it
                VERSION,
            )

        otlp_attributes = user_attributes(merged_attributes)
        if tags:
            otlp_attributes[ATTRIBUTES_TAGS_KEY] = tags

        span = self._spans_tracer.start_span(
            name=span_name_,
            attributes=otlp_attributes,
        )

        exit_stacklevel = stacklevel + (2 if decorator else 1)
        return _PendingSpan(
            LogfireSpan(
                span,
                format_args={'format_string': msg_template, 'kwargs': format_kwargs, 'stacklevel': exit_stacklevel},
            )
        )

    def span(
        self,
        msg_template: LiteralString,
        *,
        span_name: str | None = None,
        **attributes: Any,
    ) -> ContextManager[LogfireSpan]:
        """Context manager for creating a span.

        ```py
        import logfire

        with logfire.span('This is a span {a=}', a='data'):
            logfire.info('new log 1')
        ```

        Args:
            msg_template: The template for the span message.
            span_name: The span name. If not provided, the rendered message will be used.
            attributes: The arguments to format the span message template with.
        """
        return self._span(
            msg_template,
            attributes,
            span_name=span_name,
        )

    def instrument(
        self,
        msg_template: LiteralString | None = None,
        *,
        span_name: str | None = None,
        extract_args: bool | None = None,
    ) -> Callable[[Callable[_PARAMS, _RETURN]], Callable[_PARAMS, _RETURN]]:
        """Decorator for instrumenting a function as a span.

        ```py
        import logfire

        @logfire.instrument('This is a span {a=}')
        def my_function(a: int):
            logfire.info('new log {a=}', a=a)
        ```

        Args:
            msg_template: The template for the span message. If not provided, the span name will be used.
            span_name: The name of the span. If not provided, the function name will be used.
            extract_args: Whether to extract arguments from the function signature and log them as span attributes.
                If not provided, this will be enabled if `msg_template` is provided and contains `{}`.
        """
        if extract_args is None:
            extract_args = bool(msg_template and '{' in msg_template)

        def decorator(func: Callable[_PARAMS, _RETURN]) -> Callable[_PARAMS, _RETURN]:
            nonlocal span_name
            if span_name is None:
                if func.__module__:
                    span_name_ = f'{func.__module__}.{getattr(func, "__qualname__", func.__name__)}'
                else:
                    span_name_ = getattr(func, '__qualname__', func.__name__)
            else:
                span_name_ = span_name

            pos_params = ()
            if extract_args:
                sig = inspect_signature(func)
                pos_params = tuple(n for n, p in sig.parameters.items() if p.kind in _POSITIONAL_PARAMS)

            @wraps(func)
            def _instrument_wrapper(*args: _PARAMS.args, **kwargs: _PARAMS.kwargs) -> _RETURN:
                if extract_args:
                    pos_args = {k: v for k, v in zip(pos_params, args)}
                    extracted_attributes = {**pos_args, **kwargs}
                else:
                    extracted_attributes = {}

                with self._span(msg_template, extracted_attributes, span_name=span_name_, decorator=True):  # type: ignore
                    return func(*args, **kwargs)

            return _instrument_wrapper

        return decorator

    def log(
        self, level: LevelName, msg_template: LiteralString, attributes: dict[str, Any], stack_offset: int = 0
    ) -> None:
        """Log a message.

        ```py
        import logfire

        logfire.log('info', 'This is a log {a}', {'a': 'Apple'})
        ```

        Args:
            level: The level of the log.
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
            stack_offset: The stack level offset to use when collecting stack info, also affects the warning which
                message formatting might emit, defaults to `0` which means the stack info will be collected from the
                position where `logfire.log` was called.
        """
        stacklevel = stack_offset + 2
        stack_info = _get_caller_stack_info(stacklevel)

        merged_attributes = {**stack_info, **ATTRIBUTES.get(), **attributes}
        tags = _merge_tags_into_attributes(merged_attributes, self._tags) or []
        msg = logfire_format(msg_template, merged_attributes, stacklevel=stacklevel + 2)
        otlp_attributes = user_attributes(merged_attributes)
        otlp_attributes = {
            ATTRIBUTES_SPAN_TYPE_KEY: 'log',
            ATTRIBUTES_LOG_LEVEL_KEY: level,
            ATTRIBUTES_MESSAGE_TEMPLATE_KEY: msg_template,
            ATTRIBUTES_MESSAGE_KEY: msg,
            **otlp_attributes,
        }
        if tags:
            otlp_attributes[ATTRIBUTES_TAGS_KEY] = tags

        start_time = self._config.ns_timestamp_generator()

        if self._logs_tracer is None:
            self._logs_tracer = self._get_tracer_provider().get_tracer(
                'logfire',  # the name here is really not important, logfire itself doesn't use it
                VERSION,
                wrap_with_start_span_tracer=False,  # logs don't need a start span
            )

        span = self._logs_tracer.start_span(
            msg,
            attributes=otlp_attributes,
            start_time=start_time,
        )
        with trace_api.use_span(span, end_on_exit=False, record_exception=False):
            span.set_status(trace_api.Status(trace_api.StatusCode.OK))
            span.end(start_time)

    def debug(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a debug message.

        ```py
        import logfire

        logfire.debug('This is a debug log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('debug', msg_template, attributes, stack_offset=1)

    def info(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log an info message.

        ```py
        import logfire

        logfire.info('This is an info log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('info', msg_template, attributes, stack_offset=1)

    def notice(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a notice message.

        ```py
        import logfire

        logfire.notice('This is a notice log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('notice', msg_template, attributes, stack_offset=1)

    def warning(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a warning message.

        ```py
        import logfire

        logfire.warning('This is a warning log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('warning', msg_template, attributes, stack_offset=1)

    def error(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log an error message.

        ```py
        import logfire

        logfire.error('This is an error log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('error', msg_template, attributes, stack_offset=1)

    def critical(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a critical message.

        ```py
        import logfire

        logfire.critical('This is a critical log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        self.log('critical', msg_template, attributes, stack_offset=1)

    def force_flush(self, timeout_millis: int = 3_000) -> bool:
        """Force flush all spans.

        Args:
            timeout_millis: The timeout in milliseconds.

        Returns:
            Whether the flush was successful.
        """
        return self._get_tracer_provider().force_flush(timeout_millis)


class LogfireSpan(ReadableSpan):
    def __init__(self, span: trace_api.Span, format_args: _FormatArgs) -> None:
        self._span = span
        self._format_args = format_args
        self.end_on_exit = True

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            return getattr(self._span, name)

    @property
    def message_template(self) -> str | None:
        attributes = getattr(self._span, 'attributes')
        if not attributes:
            return None
        if ATTRIBUTES_MESSAGE_TEMPLATE_KEY not in attributes:
            return None
        return str(attributes[ATTRIBUTES_MESSAGE_TEMPLATE_KEY])

    @property
    def tags(self) -> Sequence[str]:
        attributes = getattr(self._span, 'attributes')
        if not attributes:
            return []
        if ATTRIBUTES_TAGS_KEY not in attributes:
            return []
        return cast(Sequence[str], attributes[ATTRIBUTES_TAGS_KEY])

    def end(self) -> None:
        """Sets the current time as the span's end time.

        The span's end time is the wall time at which the operation finished.

        Only the first call to this method is recorded, further calls are ignored so you
        can call this within the span's context manager to end it before the context manager
        exits.
        """
        if self._span.is_recording():
            self._span.end()

    def activate(self, end_on_exit: bool | None = None) -> ContextManager[LogfireSpan]:
        """
        Activates this span in the current context.

        Args:
            end_on_exit: Whether to end the span when the context manager exits, if `None` will use the value
                of self.end_on_exit.
                By setting `end_on_exit=False` when creating the span or assigning the attribute you can
                later use `activate` to manually activate and end the span.
        """
        return _PendingSpan(self, end_on_exit=end_on_exit)

    def set_attribute(self, key: str, value: otel_types.AttributeValue) -> None:
        """Sets an attribute on the span.

        Args:
            key: The key of the attribute.
            value: The value of the attribute.
        """
        self._span.set_attribute(key, value)
        self._format_args['kwargs'][key] = value


ATTRIBUTES: ContextVar[dict[str, Any]] = ContextVar('logfire.attributes', default={})


class _FormatArgs(TypedDict):
    format_string: LiteralString
    kwargs: dict[str, Any]
    stacklevel: int


@dataclass(**({'slots': True} if sys.version_info >= (3, 10) else {}))
class _PendingSpan:
    """Context manager for applying a span into the current context.

    By using a class instead of `@contextmanager` we avoid stack frames
    from the SDK in the tracebacks captured by logfire (contextlib's
    implementation starts injecting additional stack frames into the
    traceback).
    """

    span: LogfireSpan
    end_on_exit: bool | None = None
    token: None | object = field(init=False, default=None)
    # past tokens, in case of reentrant entry
    tokens_stack: None | list[object] = field(init=False, default=None)

    def __enter__(self) -> LogfireSpan:
        sdk_span = self.span._span  # type: ignore[reportPrivateUsage]
        token = context_api.attach(trace_api.set_span_in_context(sdk_span))
        if self.token is not None:
            if self.tokens_stack is None:
                self.tokens_stack = [self.token]
            else:
                self.tokens_stack.append(self.token)
        self.token = token

        return self.span

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: Any) -> None:
        context_api.detach(self.token)

        if self.tokens_stack:
            self.token = self.tokens_stack.pop()
        else:
            self.token = None

        sdk_span = self.span._span  # type: ignore[reportPrivateUsage]

        if sdk_span.is_recording():
            # record exception if present
            # isinstance is to ignore BaseException
            if exc_type is not None and isinstance(exc_value, Exception):
                # stolen from OTEL's codebase
                sdk_span.set_status(
                    trace_api.Status(
                        status_code=trace_api.StatusCode.ERROR,
                        description=f'{exc_type.__name__}: {exc_value}',
                    )
                )
                # insert a more detailed breakdown of pydantic errors
                tb = rich.traceback.Traceback.from_exception(exc_type, exc_value, traceback)
                tb.trace.stacks = [_filter_frames(stack) for stack in tb.trace.stacks]
                attributes: dict[str, otel_types.AttributeValue] = {
                    'exception.logfire.trace': json_dumps_traceback(tb.trace),
                }
                if ValidationError is not None and isinstance(exc_value, ValidationError):
                    err_json = exc_value.json(include_url=False)
                    sdk_span.set_attribute(ATTRIBUTES_VALIDATION_ERROR_KEY, exc_value.json(include_url=False))
                    attributes[ATTRIBUTES_VALIDATION_ERROR_KEY] = err_json
                sdk_span.record_exception(exc_value, attributes=attributes, escaped=True)
            else:
                sdk_span.set_status(
                    trace_api.Status(
                        status_code=trace_api.StatusCode.OK,
                    )
                )

        # We allow attributes to be set while the span is active, so we need to
        # reformat the message in case any new attributes were added.
        format_args = self.span._format_args  # type: ignore[reportPrivateUsage]
        log_message = logfire_format(
            format_string=format_args['format_string'],
            kwargs=format_args['kwargs'],
            stacklevel=format_args['stacklevel'],
        )
        sdk_span.set_attribute(ATTRIBUTES_MESSAGE_KEY, log_message)

        end_on_exit_ = self.span.end_on_exit if self.end_on_exit is None else self.end_on_exit
        if end_on_exit_:
            self.span.end()

        self.token = None


@contextmanager
@staticmethod
def with_attributes(**attributes: Any) -> Iterator[None]:
    """Context manager for binding attributes to all logs and traces.

    ```py
    import logfire

    with logfire.with_attributes(user_id='123'):
        logfire.info('new log 1')
    ```

    Args:
        attributes: The attributes to bind.
    """
    old_attributes = ATTRIBUTES.get()
    ATTRIBUTES.set({**old_attributes, **attributes})
    try:
        yield
    finally:
        ATTRIBUTES.set(old_attributes)


@contextmanager
@staticmethod
def with_tags(*tags: str) -> Iterator[None]:
    """Context manager for binding tags to all logs and traces.

    ```py
    import logfire

    with logfire.with_tags('tag1', 'tag2'):
        logfire.info('new log 1')
    ```

    Args:
        tags: The tags to bind.
    """
    old_attributes = ATTRIBUTES.get()
    merged_tags = _merge_tags_into_attributes(old_attributes, list(tags))
    if merged_tags:
        ATTRIBUTES.set({**old_attributes, ATTRIBUTES_TAGS_KEY: merged_tags})
    try:
        yield
    finally:
        ATTRIBUTES.set(old_attributes)


AttributesValueType = TypeVar('AttributesValueType', bound=Union[Any, otel_types.AttributeValue])


def _merge_tags_into_attributes(attributes: dict[str, Any], tags: list[str]) -> list[str] | None:
    # merge tags into attributes preserving any existing tags
    if not tags:
        return None
    if ATTRIBUTES_TAGS_KEY in attributes:
        return cast('list[str]', attributes[ATTRIBUTES_TAGS_KEY]) + tags
    return tags


def user_attributes(attributes: dict[str, Any], should_flatten: bool = True) -> dict[str, otel_types.AttributeValue]:
    """Prepare attributes for sending to OpenTelemetry.

    This will convert any non-OpenTelemetry compatible types to JSON.
    """
    prepared: dict[str, otel_types.AttributeValue] = {}
    null_args: list[str] = []

    for key, value in attributes.items():
        if value is None:
            null_args.append(key)
        elif isinstance(value, int):
            if value > OTLP_MAX_INT_SIZE:
                prepared[key + OTLP_LARGE_INT_SUFFIX] = str(value)
            else:
                prepared[key] = value
        elif isinstance(value, (str, bool, float)):
            prepared[key] = value
        elif isinstance(value, Flatten) and should_flatten:
            value = cast('Flatten[Mapping[Any, Any] | Sequence[Any]]', value).value
            iter = value.items() if isinstance(value, Mapping) else enumerate(value)
            for k, v in iter:
                inner_prepared = user_attributes({str(k): v}, should_flatten=False)
                for inner_key, inner_value in inner_prepared.items():
                    prepared[f'{key}.{inner_key}'] = inner_value
        else:
            prepared[key + NON_SCALAR_VAR_SUFFIX] = logfire_json_dumps(value)

    if null_args:
        prepared[NULL_ARGS_KEY] = tuple(null_args)

    return prepared


StackInfo = TypedDict('StackInfo', {'code.filepath': str, 'code.lineno': int, 'code.function': str}, total=False)


def _get_caller_stack_info(stacklevel: int = 3) -> StackInfo:
    """Get the stack info of the caller.

    This is used to bind the caller's stack info to logs and spans.

    Args:
        stacklevel: The stack level to get the info from.

    Returns:
        A dictionary of stack info attributes.
    """
    try:
        frame = inspect.currentframe()
        if frame is None:
            return {}
        stack = inspect.getouterframes(frame, 3)
        if len(stack) < 4:
            return {}
        caller_frame = stack[stacklevel]
        file = Path(caller_frame.filename)
        if file.is_absolute():
            try:
                file = file.relative_to(_CWD)
            except ValueError:
                # happens if filename path is not within CWD
                pass
        return {
            'code.filepath': str(file),
            'code.lineno': caller_frame.lineno,
            'code.function': caller_frame.function,
        }
    except Exception:
        return {}


def _filter_frames(stack: rich.traceback.Stack) -> rich.traceback.Stack:
    """Filter out the `record_exception` call itself."""
    stack.frames = [f for f in stack.frames if not (f.filename.endswith('logfire/_main.py') and f.name.startswith('_'))]
    return stack


_RETURN = TypeVar('_RETURN')
_PARAMS = ParamSpec('_PARAMS')
_POSITIONAL_PARAMS = {SignatureParameter.POSITIONAL_ONLY, SignatureParameter.POSITIONAL_OR_KEYWORD}
