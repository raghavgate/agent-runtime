import copy
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from app import Application
from conversations import ConversationStore, ConversationBusy, ConversationMismatch
from demo_model import DemoModel
from openai_model import OpenAIModel, ModelError
from runtime import RunError
from tracing import TraceStore


class Item(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def response(text, output=None):
    if output is None:
        output = [Item(type='message', role='assistant', id='msg-1', status='completed',
                       phase='final_answer', content=[{'type': 'output_text', 'text': text, 'annotations': []}])]
    return SimpleNamespace(status='completed', output=output, output_text=text,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.app = self.application()

    def application(self):
        app = Application('config.synthetic.json')
        app.traces = TraceStore(self.directory / 'runs')
        app.conversations = ConversationStore(self.directory / 'conversations')
        return app

    def tearDown(self):
        self.temporary.cleanup()

    def test_clarification_and_followup_survive_application_reload(self):
        conversation = self.app.create_conversation()
        identifier = conversation['conversation_id']
        first = self.app.send_message(identifier, 'Find open tickets for Northstar')
        self.assertIn('Which account', first.answer)
        reloaded = self.application()
        second = reloaded.send_message(identifier, 'Labs')
        self.assertIn('Northstar Labs', second.answer)
        self.assertEqual(second.tool_calls, 1)
        third = self.application().send_message(identifier, 'Who owns it?')
        self.assertIn('Sam Rivera', third.answer)
        self.assertIn('[crm:accounts/acct-002]', third.answer)
        self.assertEqual(third.tool_calls, 0)
        detail = self.app.conversation_detail(identifier)
        self.assertEqual(len(detail['turns']), 3)
        self.assertNotIn('model_state', detail)
        self.assertNotIn('history', detail)
        path = self.app.conversations.directory / (identifier + '.json')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_conversations_do_not_share_account_context(self):
        first = self.app.create_conversation()['conversation_id']
        second = self.app.create_conversation()['conversation_id']
        self.app.send_message(first, 'Find open tickets for Cedar & Finch')
        answer = self.app.send_message(second, 'Who is the owner?').answer
        self.assertNotIn('Maya Chen', answer)
        self.assertEqual(len(self.app.conversation_detail(first)['turns']), 1)

    def test_duplicate_request_returns_saved_answer_without_model(self):
        identifier = self.app.create_conversation()['conversation_id']
        request_id = uuid4().hex
        first = self.app.send_message(identifier, 'Find open tickets for Cedar & Finch', request_id=request_id)
        with patch.object(self.app, 'model') as model:
            second = self.app.send_message(identifier, 'Find open tickets for Cedar & Finch', request_id=request_id)
        model.assert_not_called()
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.app.conversation_detail(identifier)['turns']), 1)
        with self.assertRaises(ValueError):
            self.app.send_message(identifier, 'Different text', request_id=request_id)

    def test_failed_turn_is_visible_but_rolls_back_model_state(self):
        identifier = self.app.create_conversation()['conversation_id']
        self.app.send_message(identifier, 'Find open tickets for Northstar Labs')
        before = copy.deepcopy(self.app.conversations.get(identifier)['model_state'])

        class Failure(DemoModel):
            def generate(self, *args):
                raise ValueError('Simulated provider failure')

        request_id = uuid4().hex
        with patch.object(self.app, 'model', return_value=Failure()), self.assertRaises(RunError):
            self.app.send_message(identifier, 'A failed question', request_id=request_id)
        self.assertEqual(before, self.app.conversations.get(identifier)['model_state'])
        with patch.object(self.app, 'model') as model, self.assertRaises(RunError):
            self.app.send_message(identifier, 'A failed question', request_id=request_id)
        model.assert_not_called()
        answer = self.app.send_message(identifier, 'Who is the owner?')
        self.assertIn('Sam Rivera', answer.answer)
        self.assertEqual([turn['status'] for turn in self.app.conversation_detail(identifier)['turns']],
                         ['completed', 'failed', 'completed'])

    def test_workspace_or_model_change_blocks_reuse_before_model_request(self):
        identifier = self.app.create_conversation()['conversation_id']
        self.app.config_hash = 'changed'
        with patch.object(self.app, 'model') as model, self.assertRaises(ConversationMismatch):
            self.app.send_message(identifier, 'Question')
        model.assert_not_called()
        self.assertEqual(self.app.list_conversations(), [])

    def test_concurrent_turn_is_rejected_without_model_request(self):
        identifier = self.app.create_conversation()['conversation_id']
        with self.app.conversations.locked(identifier):
            with patch.object(self.app, 'model') as model, self.assertRaises(ConversationBusy):
                self.app.send_message(identifier, 'Question')
        model.assert_not_called()

    def test_turn_limit_blocks_more_requests(self):
        identifier = self.app.create_conversation()['conversation_id']
        self.app.conversation_max_turns = 1
        self.app.send_message(identifier, 'Find open tickets for Cedar & Finch')
        with patch.object(self.app, 'model') as model, self.assertRaises(ValueError):
            self.app.send_message(identifier, 'Who is the owner?')
        model.assert_not_called()

    def test_invalid_identifiers_and_empty_messages_are_rejected(self):
        with self.assertRaises(ValueError):
            self.app.conversations.get('../config')
        identifier = self.app.create_conversation()['conversation_id']
        for message in ('', '   ', None, 3):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.app.send_message(identifier, message)
        with self.assertRaises(ValueError):
            self.app.send_message(identifier, 'Question', request_id='invalid')

    def test_conversation_runs_do_not_flood_single_question_history(self):
        identifier = self.app.create_conversation()['conversation_id']
        self.app.send_message(identifier, 'Find open tickets for Cedar & Finch')
        self.assertEqual(self.app.traces.list_runs(), [])
        self.assertEqual(len(self.app.list_conversations()), 1)


class ProviderConversationTests(unittest.TestCase):
    def test_wire_history_preserves_phase_reasoning_and_new_user_exactly_once(self):
        client = SimpleNamespace(responses=Mock())
        reasoning = Item(type='reasoning', id='rs-1', encrypted_content='opaque-state', summary=[])
        question = response('Which Northstar?')
        question.output.insert(0, reasoning)
        call = Item(type='function_call', id='fc-1', call_id='call-1', name='lookup', arguments='{"id":17}')
        client.responses.create.side_effect = [question, response('', [call]), response('Found Labs')]
        first = OpenAIModel('mock-model', client=client)
        first.generate('Find Northstar')
        state = first.export_state()
        resumed = OpenAIModel('mock-model', client=client)
        resumed.start_turn('Labs', state)
        resumed.generate('Labs')
        result = resumed.generate('Labs', {'data': [17]})
        self.assertEqual(result['content'], 'Found Labs')
        payload = client.responses.create.call_args.kwargs['input']
        self.assertEqual(sum(item.get('role') == 'user' and item.get('content') == 'Labs' for item in payload), 1)
        self.assertTrue(any(item.get('encrypted_content') == 'opaque-state' for item in payload))
        self.assertTrue(any(item.get('phase') == 'final_answer' for item in payload))
        self.assertEqual(payload[-1]['call_id'], 'call-1')
        self.assertEqual(state['history'][-1]['type'], 'message')

    def test_context_cap_prevents_api_call(self):
        client = SimpleNamespace(responses=Mock())
        model = OpenAIModel('mock', client=client, max_history_bytes=1)
        with self.assertRaises(ModelError):
            model.generate('Question')
        client.responses.create.assert_not_called()

    def test_incompatible_state_and_pending_calls_are_not_saved(self):
        client = SimpleNamespace(responses=Mock())
        model = OpenAIModel('mock', client=client)
        with self.assertRaises(ModelError):
            model.start_turn('Question', {'version': 2, 'history': []})
        model.pending_call_id = 'unfinished'
        with self.assertRaises(ModelError):
            model.export_state()


if __name__ == '__main__':
    unittest.main()
