"""#117 create/abort regressions, adopted from the author's frozen preparation."""
import json
import socket
import subprocess

import anthropic
import httpx
import httpx2
import openai
import pytest

from lohra.agent.client import (
    AnthropicClient, OpenAIClient, ResponsesClient, assemble_anthropic_stream,
    assemble_responses_stream, assemble_streamed_response,
)
from lohra.agent.stream_abort import is_aborted


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('external network/process forbidden in author preparation')
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(subprocess, 'Popen', blocked)


def _body(http, payload, closes):
    class Body(http.SyncByteStream):
        def __iter__(self):
            yield payload

        def close(self):
            closes.append(True)
    return Body()


def _sse(events, *, typed=True):
    return ''.join((f'event: {event["type"]}\n' if typed else '') +
                   f'data: {json.dumps(event)}\n\n' for event in events).encode()


def _anthropic_prefix():
    return [
        {'type': 'message_start', 'message': {'id': 'msg_test', 'type': 'message',
         'role': 'assistant', 'model': 'synthetic', 'content': [], 'stop_reason': None,
         'stop_sequence': None, 'usage': {'input_tokens': 11, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'LAST'}},
    ]


@pytest.mark.parametrize('kind', ['chat', 'anthropic', 'responses'])
def test_abort_from_last_text_callback_beats_missing_terminal(kind):
    seen, requests, closes = [], [], []
    interrupted = False
    events = (_anthropic_prefix() if kind == 'anthropic' else
              [{'type': 'response.output_text.delta', 'sequence_number': 0,
                'item_id': 'msg_test', 'output_index': 0, 'content_index': 0, 'delta': 'LAST'}]
              if kind == 'responses' else
              [{'id': 'chatcmpl_test', 'object': 'chat.completion.chunk', 'created': 1,
                'model': 'synthetic', 'choices': [{'index': 0, 'delta': {'content': 'LAST'},
                                                 'finish_reason': None}]}])
    http = httpx2 if kind == 'anthropic' else httpx

    def handle(request):
        requests.append(True)
        return http.Response(200, headers={'content-type': 'text/event-stream'},
            stream=_body(http, _sse(events, typed=kind != 'chat'), closes))

    transport = http.Client(transport=http.MockTransport(handle), trust_env=False)
    sdk_type = anthropic.Anthropic if kind == 'anthropic' else openai.OpenAI
    sdk = sdk_type(api_key='synthetic', base_url='https://synthetic.invalid',
                   http_client=transport, max_retries=0)

    def on_text(text):
        nonlocal interrupted
        seen.append(text)
        interrupted = True

    try:
        if kind == 'anthropic':
            with sdk.messages.stream(model='synthetic', max_tokens=16,
                    messages=[{'role': 'user', 'content': 'A'}]) as stream:
                result = assemble_anthropic_stream(stream, on_text=on_text,
                                                   abort_check=lambda: interrupted)
        elif kind == 'chat':
            stream = sdk.chat.completions.create(model='synthetic', stream=True,
                messages=[{'role': 'user', 'content': 'A'}])
            result = assemble_streamed_response(stream, on_text=on_text,
                                                 abort_check=lambda: interrupted)
        else:
            stream = sdk.responses.create(model='synthetic', input='A', stream=True)
            result = assemble_responses_stream(stream, on_text=on_text,
                                               abort_check=lambda: interrupted)
    finally:
        sdk.close()
        transport.close()
    assert requests == closes == [True] and seen == ['LAST'] and interrupted
    assert is_aborted(result), ('last callback abort lost', kind, result)


@pytest.mark.parametrize('kind', ['chat', 'anthropic'])
def test_true_nonstreaming_create_does_not_need_stream_event_markers(kind):
    requests, closes = [], []
    http = httpx2 if kind == 'anthropic' else httpx
    payload = ({'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'synthetic',
                'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn',
                'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 1}}
               if kind == 'anthropic' else
               {'id': 'chatcmpl_test', 'object': 'chat.completion', 'created': 1,
                'model': 'synthetic', 'choices': [{'index': 0, 'finish_reason': 'stop',
                 'message': {'role': 'assistant', 'content': 'OK'}}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}})

    def handle(request):
        requests.append(json.loads(request.content))
        return http.Response(200, headers={'content-type': 'application/json'},
            stream=_body(http, json.dumps(payload).encode(), closes))

    transport = http.Client(transport=http.MockTransport(handle), trust_env=False)
    sdk_type = anthropic.Anthropic if kind == 'anthropic' else openai.OpenAI
    sdk = sdk_type(api_key='synthetic', base_url='https://synthetic.invalid',
                   http_client=transport, max_retries=0)
    wrapper = (AnthropicClient if kind == 'anthropic' else OpenAIClient).__new__(
               AnthropicClient if kind == 'anthropic' else OpenAIClient)
    wrapper._client = sdk
    try:
        result = wrapper.create(model='synthetic', max_tokens=16,
                                messages=[{'role': 'user', 'content': 'A'}])
    finally:
        wrapper.close()
        transport.close()
    assert len(requests) == 1 and not requests[0].get('stream') and closes == [True]
    assert (result.content[0].text if kind == 'anthropic' else result.choices[0].message.content) == 'OK'


@pytest.mark.parametrize('terminal', [False, True])
def test_responses_create_still_requires_its_internal_stream_terminal(terminal):
    requests, closes = [], []
    events = [{'type': 'response.output_item.done', 'sequence_number': 0, 'output_index': 0,
               'item': {'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text', 'text': 'OK',
                                                          'annotations': []}]}}]
    if terminal:
        events.append({'type': 'response.completed', 'sequence_number': 1, 'response': {
            'id': 'resp_test', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'synthetic', 'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1,
                                                       'total_tokens': 3}}})

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=_body(httpx, _sse(events), closes))

    transport = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
    sdk = openai.OpenAI(api_key='synthetic', base_url='https://synthetic.invalid',
                        http_client=transport, max_retries=0)
    wrapper = ResponsesClient.__new__(ResponsesClient)
    wrapper._client, wrapper._credential_headers = sdk, None
    result, error = None, None
    try:
        result = wrapper.create(model='synthetic', input='A')
    except Exception as exc:
        assert not isinstance(exc, (TypeError, AttributeError)), repr(exc)
        error = exc
    finally:
        wrapper.close()
        transport.close()
    assert len(requests) == 1 and requests[0]['stream'] is True and closes == [True]
    if terminal:
        assert error is None and result['output'][0].content[0].text == 'OK'
        assert result['usage'].input_tokens == 2 and result['usage'].output_tokens == 1
    else:
        assert error is not None and result is None, ('uncertified internal stream', result)
        assert isinstance(error, ValueError) and 'terminal' in str(error)
