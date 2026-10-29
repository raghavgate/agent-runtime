import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import Application, ROOT
from evaluations.runner import evaluate, evaluate_saved, grade, compare_reports
from integrations.import_crm import import_crm
from pricing import estimate, rates_for
from runtime import RunError, Runtime
from tracing import RunRecord


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.app = Application()
        self.app.traces.directory = self.directory / 'runs'
        self.app.evaluation_dir = self.directory / 'reports'

    def tearDown(self):
        self.temporary.cleanup()

    def test_quick_profile_and_stable_baseline(self):
        first = evaluate(self.app, profile='quick')
        second = evaluate(self.app, profile='quick')
        self.assertEqual(first['summary']['total'], 3)
        self.assertEqual(first['limits']['max_tool_calls'], 4)
        self.assertEqual(first['baseline_id'], first['evaluation_id'])
        self.assertEqual(second['baseline_id'], first['evaluation_id'])
        self.assertEqual(second['comparison']['pass_rate_delta'], 0)
        self.assertEqual(self.app.traces.list_runs(), [])

    def test_typographic_apostrophe_is_not_a_failure(self):
        record = RunRecord('question', 'test', 'test')
        record.status = 'completed'
        record.answer = 'I couldn’t find any CRM account matching Umbrella.'
        self.assertTrue(grade(record, {'answer_patterns': ["couldn't find"]})['answer_fact_0'])

    def test_replay_does_not_construct_a_model(self):
        report = evaluate(self.app, profile='quick')
        manifest = self.directory / 'manifest.json'
        manifest.write_text(json.dumps({row['case_id']: row['run_id'] for row in report['results']}))
        with patch.object(self.app, 'model') as model:
            replay = evaluate_saved(self.app, manifest)
        model.assert_not_called()
        self.assertEqual(replay['summary']['passed'], 3)
        self.assertTrue(replay['replayed'])

    def test_changed_grader_is_not_reported_as_model_improvement(self):
        report = evaluate(self.app, profile='quick')
        changed = copy.deepcopy(report)
        changed['grader_hash'] = 'different'
        self.assertFalse(compare_reports(report, changed)['comparable'])

    def test_cost_guard_stops_before_next_case(self):
        record = RunRecord('question', 'demo', 'demo')
        record.status = 'completed'
        record.answer = 'answer'
        record.estimated_cost_usd = 0.06
        with patch.object(self.app, 'run', return_value=record) as run:
            report = evaluate(self.app, profile='quick', max_cost_usd=0.05)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(report['summary']['complete'])
        self.assertEqual(report['summary']['planned'], 3)
        self.assertIn('spend', report['stopped_reason'])
        self.assertIsNone(report['baseline_id'])

    def test_provider_failure_stops_suite(self):
        record = RunRecord('question', 'openai', 'mock')
        record.status = 'failed'
        rates = {'input_usd_per_million': 1, 'output_usd_per_million': 1}
        with patch.object(self.app, 'model', return_value=SimpleNamespace(model_name='mock')), patch.object(self.app, 'rates', return_value=rates), patch.object(self.app, 'run', side_effect=RunError('failed', record)) as run:
            report = evaluate(self.app, provider='openai', profile='quick')
        self.assertEqual(run.call_count, 1)
        self.assertIn('Provider failed', report['stopped_reason'])
        self.assertIsNone(report['summary']['estimated_cost_usd'])

    def test_rates_are_model_scoped(self):
        self.assertIsNone(rates_for(self.app.config, 'another-model'))
        rates = self.app.rates('gpt-5.4-mini')
        self.assertAlmostEqual(estimate({'input_tokens': 2203, 'output_tokens': 160}, rates), 0.00237225)
        self.assertIsNone(estimate(None, rates))

    def test_workspace_requirements_reach_model_without_changing_runtime(self):
        self.app.config['workflow_instructions'] = 'Include open tickets in an account brief.'
        with patch('openai_model.OpenAIModel') as adapter:
            self.app.model('openai', 'test-model')
        instructions = adapter.call_args.kwargs['instructions']
        self.assertIn('Include open tickets in an account brief.', instructions)
        self.assertIn('Never invent customer IDs', instructions)

    def test_invalid_profile_repeat_and_spend_fail_before_model(self):
        for kwargs in ({'profile': []}, {'repeat': 0}, {'repeat': 21}, {'max_cost_usd': float('nan')}, {'max_cost_usd': -1}):
            with patch.object(self.app, 'model') as model, self.assertRaises(ValueError):
                evaluate(self.app, **kwargs)
            model.assert_not_called()

    def test_token_guard_prevents_followup(self):
        class Model:
            last_usage = None
            count = 0

            def generate(self, *args):
                self.count += 1
                self.last_usage = {'input_tokens': 20, 'output_tokens': 10}
                return {'type': 'tool_call', 'tool_name': 'crm_search_accounts', 'arguments': {'company_name': 'Acme'}}
        model = Model()
        with self.assertRaises(RunError):
            Runtime(self.app.registry, max_total_tokens=30).run(model, 'Acme')
        self.assertEqual(model.count, 1)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.source = ROOT / 'data' / 'crm_export.example.csv'
        self.mapping = ROOT / 'data' / 'crm_mapping.example.json'

    def tearDown(self):
        self.temporary.cleanup()

    def test_import_is_isolated_and_support_is_not_fabricated(self):
        result = import_crm(self.source, self.directory / 'workspace', self.mapping)
        app = Application(result['config'])
        self.assertEqual(len(app.registry.list_tools()), 2)
        account = app.registry.get('crm_search_accounts').invoke({'company_name': 'Example Company'})['data'][0]
        self.assertEqual(account['customer_id'], 'acct-example-001')
        self.assertEqual(account['support_customer_id'], '')
        self.assertEqual(app.registry.get('crm_get_account').invoke({'customer_id': 'acct-example-001'})['data'][0], account)
        with self.assertRaises(ValueError):
            evaluate(app)

    def test_dry_run_does_not_write(self):
        result = import_crm(self.source, self.directory / 'never', self.mapping, dry_run=True)
        self.assertFalse(result['written'])
        self.assertFalse((self.directory / 'never').exists())

    def test_no_overwrite(self):
        import_crm(self.source, self.directory / 'workspace', self.mapping)
        with self.assertRaises(ValueError):
            import_crm(self.source, self.directory / 'workspace', self.mapping)

    def test_duplicate_invalid_date_and_nonfinite_amount_are_rejected(self):
        account = {'customer_id': 'id-1', 'company_name': 'Example', 'owner': 'Owner',
                   'renewal_date': '2027-01-01', 'annual_value': 12, 'support_customer_id': ''}
        cases = [[account, account], [dict(account, renewal_date='2027-02-31')],
                 [dict(account, annual_value='NaN')], [dict(account, annual_value=-5)],
                 [dict(account, annual_value=True)], [dict(account, company_name='')]]
        for index, rows in enumerate(cases):
            source = self.directory / f'{index}.json'
            source.write_text(json.dumps(rows))
            with self.subTest(index=index), self.assertRaises(ValueError):
                import_crm(source, dry_run=True)


if __name__ == '__main__':
    unittest.main()
