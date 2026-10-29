import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import Application, ROOT
from evaluations.runner import grade, load_cases, workspace_cases_path, evaluate
from integrations.crm import CRMIntegration
from main import main
from runtime import Runtime
from tracing import RunRecord

CONFIG = ROOT / 'config.synthetic.json'


class SequenceModel:
    """Explicit test sequence, not natural-language understanding or an LLM."""
    provider = 'scripted'
    model_name = 'fixture-protocol-check'

    def __init__(self, responses):
        self.responses = responses

    def reset(self):
        self.iterator = iter(self.responses)

    def generate(self, *args):
        return next(self.iterator)


def call(name, **arguments):
    return {'type': 'tool_call', 'tool_name': name, 'arguments': arguments}


class SyntheticDataTests(unittest.TestCase):
    def setUp(self):
        self.app = Application(CONFIG)

    def invoke(self, name, **arguments):
        return self.app.registry.get(name).invoke(arguments)

    def test_counts_and_manifest_hashes(self):
        expected = {'accounts': 24, 'contacts': 69, 'deals': 48, 'support_organizations': 23, 'tickets': 88}
        self.assertEqual(self.app.validate_data()['counts'], expected)
        manifest = json.loads((ROOT / 'data/synthetic/manifest.json').read_text())
        self.assertEqual(manifest['counts'], expected)
        for name, digest in manifest['files_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT / 'data/synthetic' / name).read_bytes()).hexdigest(), digest)

    def test_contacts_are_linked_and_use_example_domain(self):
        payload = json.loads((ROOT / 'data/synthetic/crm.json').read_text())
        ids = {row['customer_id'] for row in payload['accounts']}
        self.assertTrue(all(row['customer_id'] in ids for row in payload['contacts'] + payload['deals']))
        self.assertTrue(all(row['email'] is None or row['email'].endswith('.example') for row in payload['contacts']))

    def test_independently_known_primary_contact(self):
        result = self.invoke('crm_get_contacts', customer_id='acct-001')
        primary = [row for row in result['data'] if row['is_primary']]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]['full_name'], 'Avery Chen')
        self.assertEqual(primary[0]['email'], 'avery.chen@cedar-finch.example')

    def test_missing_email_and_primary_designation_remain_missing(self):
        meridian = self.invoke('crm_get_contacts', customer_id='acct-006')['data']
        self.assertIsNone(next(row['email'] for row in meridian if row['is_primary']))
        juniper = self.invoke('crm_get_contacts', customer_id='acct-008')['data']
        self.assertEqual(len(juniper), 3)
        self.assertFalse(any(row['is_primary'] for row in juniper))
        empty = self.invoke('crm_get_contacts', customer_id='acct-005')
        self.assertEqual(empty['data'], [])
        self.assertEqual(empty['sources'][0]['id'], 'crm:contact-search/acct-005')

    def test_pipeline_totals_and_closed_exclusion(self):
        cedar = self.invoke('crm_get_deals', customer_id='acct-001', stage='OPEN')['data']
        self.assertEqual({row['deal_id'] for row in cedar}, {'deal-001-1', 'deal-001-2'})
        self.assertEqual(sum(row['amount'] for row in cedar), 90000)
        juniper = self.invoke('crm_get_deals', customer_id='acct-008', stage='OPEN')['data']
        self.assertEqual(sum(row['amount'] for row in juniper), 105000)
        self.assertEqual(self.invoke('crm_get_deals', customer_id='acct-007', stage='OPEN')['data'], [])
        closed = self.invoke('crm_get_deals', customer_id='acct-007', stage='ALL')['data']
        self.assertEqual({row['stage'] for row in closed}, {'CLOSED_WON', 'CLOSED_LOST'})

    def test_batch_reads_preserve_account_grouping_and_empty_results(self):
        contacts = self.invoke('crm_get_contacts_batch', customer_ids=['acct-001', 'acct-004', 'acct-006'])
        self.assertEqual(len(contacts['data']), 9)
        self.assertEqual({row['customer_id'] for row in contacts['data']}, {'acct-001', 'acct-004', 'acct-006'})
        deals = self.invoke('crm_get_deals_batch', customer_ids=['acct-001', 'acct-004', 'acct-006'], stage='OPEN')
        self.assertEqual(len(deals['data']), 6)
        self.assertTrue(all(row['stage'] not in ('CLOSED_WON', 'CLOSED_LOST') for row in deals['data']))
        tickets = self.invoke('support_get_open_tickets_batch', organization_ids=['org-1001', 'org-1004'])
        self.assertEqual({row['ticket_id'] for row in tickets['data']}, {10011, 10012})
        self.assertIn('support:organizations/org-1004', {source['id'] for source in tickets['sources']})
        with self.assertRaises(ValueError):
            self.invoke('crm_get_contacts_batch', customer_ids=['acct-001', 'does-not-exist'])
        with self.assertRaises(ValueError):
            self.invoke('crm_get_contacts_batch', customer_ids=[])
        with self.assertRaises(ValueError):
            self.invoke('support_get_open_tickets_batch', organization_ids=['org-1001', 'org-1001'])

    def test_renewal_window_includes_both_boundaries(self):
        result = self.invoke('crm_list_renewals', start_date='2026-11-05', end_date='2026-11-12')
        self.assertEqual([row['customer_id'] for row in result['data']], ['acct-001', 'acct-004', 'acct-006'])
        with self.assertRaises(ValueError):
            self.invoke('crm_list_renewals', start_date='2026-11-12', end_date='2026-11-05')
        with self.assertRaises(ValueError):
            self.invoke('crm_list_renewals', start_date='2026-02-31', end_date='2026-11-05')

    def test_open_tickets_exclude_closed_critical_and_pending(self):
        cedar = self.invoke('support_get_open_tickets', organization_id='org-1001')['data']
        self.assertEqual({row['ticket_id'] for row in cedar}, {10011, 10012})
        self.assertEqual(self.invoke('support_get_open_tickets', organization_id='org-1004')['data'], [])
        self.assertEqual(self.invoke('support_get_open_tickets', organization_id='org-1007')['data'], [])
        meridian = self.invoke('crm_get_account', customer_id='acct-006')['data'][0]
        self.assertEqual(meridian['support_customer_id'], '')

    def test_unknown_account_does_not_look_like_empty_contacts(self):
        with self.assertRaises(ValueError):
            self.invoke('crm_get_contacts', customer_id='does-not-exist')
        with self.assertRaises(ValueError):
            self.invoke('crm_get_deals', customer_id='does-not-exist', stage='OPEN')

    def test_old_workspace_keeps_original_tools(self):
        self.assertEqual(len(Application().registry.list_tools()), 4)
        self.assertEqual(len(self.app.registry.list_tools()), 9)

    def test_missing_collection_is_not_exposed_as_empty_data(self):
        payload = json.loads((ROOT / 'data/synthetic/crm.json').read_text())
        payload.pop('deals')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crm.json'
            path.write_text(json.dumps(payload))
            integration = CRMIntegration(path)
            names = {tool.name for tool in integration.tools()}
            self.assertIn('crm_get_contacts', names)
            self.assertNotIn('crm_get_deals', names)
            self.assertNotIn('deals', integration.summary())

    def test_scripted_cross_system_runtime(self):
        model = SequenceModel([
            call('crm_search_accounts', company_name='Cedar & Finch'),
            call('crm_get_contacts', customer_id='acct-001'),
            call('crm_get_deals', customer_id='acct-001', stage='OPEN'),
            call('support_get_open_tickets', organization_id='org-1001'),
            {'type': 'final_answer', 'content': 'Cedar & Finch [crm:accounts/acct-001]; Avery Chen [crm:contacts/contact-001-1]; $90,000 open pipeline [crm:deals/deal-001-1] [crm:deals/deal-001-2]; two open tickets [support:tickets/10011] [support:tickets/10012].'},
        ])
        record = Runtime(self.app.registry, max_tool_calls=4, require_sources=True).run_record(model, 'Test sequence')
        self.assertEqual(record.status, 'completed')
        self.assertEqual(record.tool_calls, 4)
        self.assertEqual({source['system'] for source in record.sources}, {'crm', 'support'})

    def test_meeting_preparation_contract(self):
        case = next(case for case in load_cases(workspace_cases_path(self.app), 'full')
                    if case['id'] == 'synthetic-meeting-preparation')
        model = SequenceModel([
            call('crm_search_accounts', company_name='Cedar & Finch'),
            call('crm_get_contacts', customer_id='acct-001'),
            call('crm_get_deals', customer_id='acct-001', stage='OPEN'),
            call('support_get_open_tickets', organization_id='org-1001'),
            {'type': 'final_answer', 'content': (
                'Executive snapshot: Cedar & Finch is owned by Maya Chen, renews 2026-11-05, '
                'and has $72,000 annual value [crm:accounts/acct-001].\n'
                'Primary attendee: Avery Chen, Operations Director; last contacted 2026-10-23 '
                '[crm:contacts/contact-001-1].\n'
                'Commercial position: $90,000 OPEN pipeline: NEGOTIATION $72,000 '
                '[crm:deals/deal-001-1] and PROPOSAL $18,000 [crm:deals/deal-001-2].\n'
                'Support concerns: CRITICAL invoice export issue [support:tickets/10011] and '
                'HIGH account sync issue [support:tickets/10012].\n'
                'Talking points: address both support issues and discuss the renewal and expansion.'
            )},
        ])
        record = Runtime(self.app.registry, max_tool_calls=4, require_sources=True).run_record(model, case['message'])
        checks = grade(record, case)
        self.assertTrue(all(checks.values()), checks)

    def test_renewal_attention_contract_uses_batch_reads(self):
        case = next(case for case in load_cases(workspace_cases_path(self.app), 'full')
                    if case['id'] == 'synthetic-critical-renewal-review')
        ids = ['acct-001', 'acct-004', 'acct-006']
        model = SequenceModel([
            call('crm_list_renewals', start_date='2026-11-05', end_date='2026-11-12'),
            call('crm_get_contacts_batch', customer_ids=ids),
            call('crm_get_deals_batch', customer_ids=ids, stage='OPEN'),
            call('support_get_open_tickets_batch', organization_ids=['org-1001', 'org-1004']),
            {'type': 'final_answer', 'content': (
                'Priority 1 — Cedar & Finch: owner Maya Chen, renews 2026-11-05, $72,000 annual value '
                '[crm:accounts/acct-001]. Primary Avery Chen [crm:contacts/contact-001-1]. OPEN pipeline '
                '$90,000: NEGOTIATION [crm:deals/deal-001-1] and PROPOSAL [crm:deals/deal-001-2]. '
                'OPEN support: CRITICAL [support:tickets/10011] and HIGH [support:tickets/10012]. '
                'Recommendation: escalate the critical issue before renewal.\n'
                'Priority 2 — Meridian Health: owner Priya Shah, renews 2026-11-12, $54,000 annual value '
                '[crm:accounts/acct-006]. Primary Casey Reed has email unavailable '
                '[crm:contacts/contact-006-1]. OPEN pipeline $67,500: NEGOTIATION '
                '[crm:deals/deal-006-1] and PROPOSAL [crm:deals/deal-006-2]. Support cannot be checked '
                'because its mapping is unavailable. Recommendation: verify support access and contact details.\n'
                'Priority 3 — Bluewater Systems: owner Maya Chen, renews 2026-11-10, $24,000 annual value '
                '[crm:accounts/acct-004]. Primary Riley Santos [crm:contacts/contact-004-1]. OPEN pipeline '
                '$30,000: NEGOTIATION [crm:deals/deal-004-1] and PROPOSAL [crm:deals/deal-004-2]. '
                'No open tickets were found [support:organizations/org-1004]. Recommendation: confirm renewal timing.'
            )},
        ])
        record = Runtime(self.app.registry, max_tool_calls=4, require_sources=True).run_record(model, case['message'])
        checks = grade(record, case)
        self.assertTrue(all(checks.values()), checks)

    def test_development_and_heldout_cases_are_separate(self):
        development = load_cases(workspace_cases_path(self.app), 'full')
        heldout = load_cases(workspace_cases_path(self.app, split='heldout'), 'full')
        self.assertEqual(len(development), 13)
        self.assertEqual(len(heldout), 6)
        self.assertTrue({case['id'] for case in development}.isdisjoint(case['id'] for case in heldout))
        self.assertEqual(len(load_cases(workspace_cases_path(self.app), 'quick')), 3)

    def test_demo_cannot_claim_to_pass_rich_evaluations(self):
        with patch.object(self.app, 'model') as model, self.assertRaises(ValueError):
            evaluate(self.app)
        model.assert_not_called()

    def test_generic_record_grader_rejects_wrong_account_contacts(self):
        record = RunRecord('question', 'scripted', 'test')
        record.status = 'completed'
        record.answer = 'A contact'
        record.event('tool_result', tool_name='crm_get_contacts', result={'data': [{'contact_id': 'contact-002-1'}]})
        checks = grade(record, {'expected_records': [{'tool_name': 'crm_get_contacts', 'id_field': 'contact_id', 'ids': ['contact-001-1']}]})
        self.assertFalse(checks['retrieved_records_0'])

    def test_validate_command_makes_no_model_requests(self):
        output = io.StringIO()
        with patch('openai_model.OpenAIModel') as model, redirect_stdout(output):
            self.assertEqual(main(['--config', str(CONFIG), 'validate-data']), 0)
        model.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['model_requests'], 0)

    def test_orphan_child_record_is_rejected(self):
        payload = json.loads((ROOT / 'data/synthetic/crm.json').read_text())
        payload['contacts'][0]['customer_id'] = 'missing-account'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crm.json'
            path.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                CRMIntegration(path)


if __name__ == '__main__':
    unittest.main()
