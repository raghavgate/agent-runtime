import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from app import Application
from integrations.hubspot import HubSpotAPIError, HubSpotClient, HubSpotIntegration


class FakeHubSpotClient:
    def __init__(self):
        self.calls = []
        self.company = {"id": "17", "properties": {"name": "Acme", "hubspot_owner_id": "9",
                        "renewal": "2027-01-15", "arr": "12500", "support_id": "org-17"}}
        self.contact = {"id": "31", "properties": {"firstname": "Maya", "lastname": "Chen",
                        "jobtitle": "VP Operations", "email": "maya@example.test",
                        "primary_contact": "true", "notes_last_contacted": "2026-10-20T15:00:00Z"}}
        self.deal = {"id": "44", "properties": {"dealname": "Expansion", "amount": "7000.50",
                     "deal_currency_code": "CAD", "dealstage": "qualified", "pipeline": "default",
                     "hs_is_closed": "false", "closedate": "1790812800000",
                     "hubspot_owner_id": "9", "hs_lastmodifieddate": "2026-10-27T15:00:00Z"}}

    def search(self, object_type, body):
        self.calls.append(("search", object_type, body))
        return {"companies": [self.company], "contacts": [self.contact], "deals": [self.deal]}[object_type]

    def get_object(self, object_type, object_id, properties):
        self.calls.append(("get", object_type, object_id, properties))
        if object_id == "missing":
            raise HubSpotAPIError("HubSpot API request failed with HTTP 404", 404)
        return self.company

    def owner_name(self, owner_id):
        return "Jordan Lee" if owner_id else None

    def stage_labels(self):
        return {"qualified": "Qualified to buy"}

    def check(self):
        return {"service": "HubSpot CRM", "connected": True, "api_version": "2026-09",
                "max_records_per_tool_call": 500}


class HubSpotClientTests(unittest.TestCase):
    def test_authentication_pagination_and_request_shape(self):
        requests = []

        def transport(method, url, headers, body, timeout):
            requests.append((method, url, headers, json.loads(body), timeout))
            if len(requests) == 1:
                return {"results": [{"id": "1", "properties": {}}],
                        "paging": {"next": {"after": "next-page"}}}
            return {"results": [{"id": "2", "properties": {}}]}

        client = HubSpotClient("secret-value", transport=transport)
        rows = client.search("companies", {"query": "Acme", "properties": ["name"]})
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual(requests[0][0], "POST")
        self.assertTrue(requests[0][1].endswith("/crm/objects/2026-09/companies/search"))
        self.assertEqual(requests[0][2]["Authorization"], "Bearer secret-value")
        self.assertEqual(requests[1][3]["after"], "next-page")

    def test_retries_rate_limit_without_exposing_provider_body(self):
        calls = []
        sleeps = []
        headers = Message()
        headers["Retry-After"] = "0"

        def transport(*arguments):
            calls.append(arguments)
            if len(calls) == 1:
                raise HTTPError(arguments[1], 429, "private provider detail", headers, None)
            return {"ok": True}

        client = HubSpotClient("secret-value", transport=transport, sleep=sleeps.append)
        self.assertEqual(client.request("GET", "/test"), {"ok": True})
        self.assertEqual(sleeps, [0])

    def test_http_errors_are_sanitized_and_retain_status(self):
        def transport(*arguments):
            raise HTTPError(arguments[1], 403, "sensitive response text", Message(), None)

        with self.assertRaises(HubSpotAPIError) as caught:
            HubSpotClient("secret-value", transport=transport).request("GET", "/test")
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    def test_record_limit_is_enforced_across_pages(self):
        client = HubSpotClient("secret-value", max_records=1,
                               transport=lambda *args: {"results": [{"id": "1"}, {"id": "2"}]})
        with self.assertRaisesRegex(HubSpotAPIError, "record limit"):
            client.search("companies", {})

    def test_repeated_paging_cursor_is_rejected(self):
        client = HubSpotClient("secret-value", transport=lambda *args: {
            "results": [], "paging": {"next": {"after": "same"}}})
        with self.assertRaisesRegex(HubSpotAPIError, "paging cursor"):
            client.search("companies", {})


class HubSpotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeHubSpotClient()
        self.integration = HubSpotIntegration({"kind": "hubspot_crm", "properties": {
            "renewal_date": "renewal", "annual_value": "arr", "support_customer_id": "support_id",
            "contact_primary": "primary_contact"}}, client=self.client)
        self.tools = {tool.name: tool for tool in self.integration.tools()}

    def invoke(self, name, **arguments):
        return self.tools[name].invoke(arguments)

    def test_neutral_account_contact_and_deal_contracts(self):
        account = self.invoke("crm_search_accounts", company_name="Acme")["data"][0]
        self.assertEqual(account, {"customer_id": "17", "company_name": "Acme", "owner": "Jordan Lee",
                                   "renewal_date": "2027-01-15", "annual_value": 12500,
                                   "support_customer_id": "org-17"})
        contact = self.invoke("crm_get_contacts", customer_id="17")["data"][0]
        self.assertEqual(contact["full_name"], "Maya Chen")
        self.assertTrue(contact["is_primary"])
        self.assertEqual(contact["last_contacted_date"], "2026-10-20")
        deal = self.invoke("crm_get_deals", customer_id="17", stage="OPEN")["data"][0]
        self.assertEqual(deal["amount"], 7000.5)
        self.assertEqual(deal["currency"], "CAD")
        self.assertEqual(deal["stage"], "Qualified to buy")
        filters = self.client.calls[-1][2]["filterGroups"][0]["filters"]
        self.assertIn({"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}, filters)

    def test_missing_optional_mappings_are_explicitly_unknown(self):
        integration = HubSpotIntegration({"kind": "hubspot_crm", "properties": {
            "deal_currency": None}}, client=self.client)
        tools = {tool.name: tool for tool in integration.tools()}
        self.assertNotIn("crm_list_renewals", tools)
        account = tools["crm_search_accounts"].invoke({"company_name": "Acme"})["data"][0]
        self.assertIsNone(account["renewal_date"])
        self.assertIsNone(account["annual_value"])
        contact = tools["crm_get_contacts"].invoke({"customer_id": "17"})["data"][0]
        self.assertFalse(contact["is_primary"])
        deal = tools["crm_get_deals"].invoke({"customer_id": "17", "stage": "ALL"})["data"][0]
        self.assertIsNone(deal["currency"])

    def test_renewal_search_and_batch_guards(self):
        renewal = self.invoke("crm_list_renewals", start_date="2027-01-01", end_date="2027-01-31")
        self.assertEqual(renewal["data"][0]["customer_id"], "17")
        filters = self.client.calls[-1][2]["filterGroups"][0]["filters"]
        self.assertEqual(filters[0]["propertyName"], "renewal")
        with self.assertRaises(ValueError):
            self.invoke("crm_get_contacts_batch", customer_ids=[])
        with self.assertRaises(ValueError):
            self.invoke("crm_get_deals_batch", customer_ids=["17", "17"], stage="OPEN")

    def test_only_a_real_404_becomes_correctable_not_found(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            self.invoke("crm_get_account", customer_id="missing")
        self.client.get_object = lambda *args: (_ for _ in ()).throw(HubSpotAPIError("permission denied", 403))
        with self.assertRaises(HubSpotAPIError):
            self.invoke("crm_get_account", customer_id="17")

    def test_configuration_rejects_unknown_or_missing_required_properties(self):
        for config in ({"kind": "hubspot_crm", "unexpected": True},
                       {"kind": "hubspot_crm", "properties": {"unknown": "x"}},
                       {"kind": "hubspot_crm", "properties": {"deal_closed": None}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                HubSpotIntegration(config, client=self.client)

    def test_application_composes_connector_and_checks_it_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "integrations": [{"kind": "hubspot_crm", "service_key_env": "HUBSPOT_SERVICE_KEY"}],
                "trace_dir": ".runs", "evaluation_dir": ".evaluations",
                "conversation_dir": ".conversations", "require_sources": True,
            }))
            with patch("integrations.hubspot.HubSpotClient", return_value=self.client):
                app = Application(path)
            self.assertIn("crm_search_accounts", {tool["name"] for tool in app.registry.list_tools()})
            self.assertEqual(app.dataset_counts, {})
            self.assertTrue(app.check_connections()["connected"])
            self.assertEqual(app.validate_data()["model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
