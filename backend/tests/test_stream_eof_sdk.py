"""#117 SDK/cache regressions, adopted from the coordinator's frozen preparation."""
import dataclasses
import json
import socket
import subprocess

import anthropic
import httpx
import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient, assemble_anthropic_stream, assemble_responses_stream
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('no network or process in synthetic SDK preparation')
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(subprocess, 'Popen', blocked)


def _body(http, events, closes):
    class Body(http.SyncByteStream):
        def __iter__(self):
            for event in events:
                yield (f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n').encode()

        def close(self):
            closes.append(True)

    return Body()


@pytest.mark.parametrize('stop_event,reason', [(False, 'end_turn'), (True, None), (True, 'end_turn')])
def test_anthropic_requires_both_terminal_event_and_reason(stop_event, reason):
    closes, seen, requests = [], [], []
    events = [
        {'type': 'message_start', 'message': {'id': 'msg_synthetic', 'type': 'message',
         'role': 'assistant', 'model': 'synthetic', 'content': [], 'stop_reason': None,
         'stop_sequence': None, 'usage': {'input_tokens': 11, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'text_delta', 'text': 'OBSERVED'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None},
         'usage': {'output_tokens': 5}},
    ]
    if stop_event:
        events.append({'type': 'message_stop'})

    def handle(request):
        requests.append(True)
        return httpx2.Response(200, headers={'content-type': 'text/event-stream'},
                               stream=_body(httpx2, events, closes))

    transport = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    sdk = anthropic.Anthropic(api_key='synthetic', base_url='https://synthetic.invalid',
                              http_client=transport, max_retries=0)
    error, result = None, None
    try:
        with sdk.messages.stream(model='synthetic', max_tokens=32,
                                 messages=[{'role': 'user', 'content': 'synthetic'}]) as stream:
            try:
                result = assemble_anthropic_stream(stream, on_text=seen.append)
            except Exception as exc:
                assert not isinstance(exc, (TypeError, AttributeError)), repr(exc)
                error = exc
    finally:
        sdk.close()
        transport.close()
    assert requests == [True] and closes == [True] and seen == ['OBSERVED']
    if stop_event and reason:
        assert error is None and result.stop_reason == 'end_turn'
        assert result.content[0].text == 'OBSERVED'
        assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 5)
    else:
        assert error is not None, ('uncertified success', stop_event, reason, result)
        assert isinstance(error, ValueError) and 'terminal' in str(error)


@pytest.mark.parametrize('forced', [False, True], ids=['text', 'forced-schema'])
@pytest.mark.parametrize('terminal', [False, True], ids=['missing-terminal', 'completed-terminal'])
def test_output_item_done_alone_never_certifies_a_workflow_cell(tmp_path, monkeypatch, forced, terminal):
    monkeypatch.setenv('LOHRA_HOME', str(tmp_path))
    monkeypatch.setenv('LOHRA_AUDIT', 'on')
    requests, closes = [], []

    def handle(request):
        request_json = json.loads(request.content)
        requests.append(True)
        if forced:
            item = {'id': 'fc_synthetic', 'type': 'function_call', 'call_id': 'call_synthetic',
                    'name': request_json['tool_choice']['name'], 'arguments': '{}', 'status': 'completed'}
        else:
            item = {'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'OBSERVED', 'annotations': []}]}
        events = [{'type': 'response.output_item.done', 'sequence_number': 0,
                   'output_index': 0, 'item': item}]
        if terminal:
            events.append({'type': 'response.completed', 'sequence_number': 1,
                           'response': {'id': 'resp_synthetic', 'object': 'response', 'created_at': 1,
                           'status': 'completed', 'model': 'synthetic', 'output': [],
                           'usage': {'input_tokens': 11, 'output_tokens': 5, 'total_tokens': 16}}})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=_body(httpx, events, closes))

    transport = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
    sdk = openai.OpenAI(api_key='synthetic', base_url='https://synthetic.invalid/v1',
                        http_client=transport, max_retries=0)

    class Client(ModelClient):
        def create(self, **kwargs):
            raise AssertionError('expected the real streaming path')

        def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
            return assemble_responses_stream(sdk.responses.create(**{**kwargs, 'stream': True}),
                on_text=on_text, on_reasoning=on_reasoning, abort_check=abort_check)

    profile = dataclasses.replace(get_provider_profile('openai'), api_mode='responses')

    def factory():
        return Agent(model='synthetic', provider=profile, client=Client(), max_iterations=1)

    node = {'id': 'leaf', 'type': 'agent', 'prompt': 'synthetic'}
    if forced:
        node.update(tool_less=True, schema={'type': 'object'})
    database = SessionDB(tmp_path / 'state.db')
    service = WorkflowService(base_child_factory=factory, db=database, home=tmp_path)
    try:
        accepted = service.start({'meta': {'name': 'synthetic-eof'}, 'nodes': [node]})
        assert 'run_id' in accepted, accepted
        result = service.status(accepted['run_id'], wait=True, timeout=5)
        cached = database._connection.execute('SELECT COUNT(*) FROM workflow_node_cache').fetchone()[0]
    finally:
        service.shutdown()
        database.close()
        sdk.close()
        transport.close()
    assert requests == [True] and closes == [True]
    if terminal:
        assert result['status'] == 'complete' and cached == 1, result
        assert result['outputs']['leaf'] == ({} if forced else 'OBSERVED')
    else:
        assert result['status'] != 'complete' and cached == 0, (result, cached)
        assert result['faults']
        assert 'terminal' in json.dumps(result['faults'])
