"""Read-only HubSpot CRM adapter using Service Key bearer authentication."""
import json
import math
import os
import re
import time
from copy import deepcopy
from datetime import date, datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from integrations.common import envelope_schema, result, source
from tool import Tool, ToolCallError


HUBSPOT_API_ROOT = "https://api.hubapi.com"
DEFAULT_API_VERSION = "2026-09"
ACCOUNT_ID = {"type": "string", "minLength": 1}
NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}
ACCOUNT_SCHEMA = {"customer_id": ACCOUNT_ID, "company_name": "string", "owner": NULLABLE_STRING,
                  "renewal_date": NULLABLE_STRING, "annual_value": NULLABLE_NUMBER,
                  "support_customer_id": NULLABLE_STRING}
CONTACT_SCHEMA = {"contact_id": "string", "customer_id": ACCOUNT_ID, "full_name": "string",
                  "role": NULLABLE_STRING, "email": NULLABLE_STRING, "is_primary": "boolean",
                  "last_contacted_date": NULLABLE_STRING}
DEAL_SCHEMA = {"deal_id": "string", "customer_id": ACCOUNT_ID, "name": "string",
               "amount": NULLABLE_NUMBER, "currency": NULLABLE_STRING, "stage": "string",
               "expected_close_date": NULLABLE_STRING, "owner": NULLABLE_STRING,
               "updated_at": NULLABLE_STRING}

PROPERTY_DEFAULTS = {
    "company_name": "name", "company_owner": "hubspot_owner_id", "renewal_date": None,
    "annual_value": None, "support_customer_id": None,
    "contact_first_name": "firstname", "contact_last_name": "lastname", "contact_role": "jobtitle",
    "contact_email": "email", "contact_primary": None, "contact_last_contacted": "notes_last_contacted",
    "deal_name": "dealname", "deal_amount": "amount", "deal_stage": "dealstage",
    "deal_pipeline": "pipeline", "deal_closed": "hs_is_closed", "deal_close_date": "closedate",
    "deal_owner": "hubspot_owner_id", "deal_updated": "hs_lastmodifieddate",
    "deal_currency": "deal_currency_code",
}


class HubSpotAPIError(RuntimeError):
    """Sanitized provider failure; response bodies and credentials are never retained."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class HubSpotClient:
    def __init__(self, service_key, api_version=DEFAULT_API_VERSION, timeout=15, max_records=500,
                 transport=None, sleep=time.sleep):
        if not isinstance(service_key, str) or not service_key.strip():
            raise ValueError("HubSpot Service Key is missing")
        if not re.fullmatch(r"\d{4}-\d{2}", api_version):
            raise ValueError("HubSpot api_version must use YYYY-MM format")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise ValueError("HubSpot timeout_seconds must be between 1 and 60")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 10000:
            raise ValueError("HubSpot max_records must be between 1 and 10000")
        self.service_key = service_key.strip()
        self.api_version = api_version
        self.timeout = timeout
        self.max_records = max_records
        self.transport = transport or self._urlopen_transport
        self.sleep = sleep
        self._owners = {}
        self._stages = None

    @staticmethod
    def _urlopen_transport(method, url, headers, body, timeout):
        request = Request(url, data=body, headers=headers, method=method)
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HubSpotAPIError("HubSpot returned invalid JSON") from None

    def request(self, method, path, query=None, body=None):
        url = HUBSPOT_API_ROOT + path
        if query:
            url += "?" + urlencode(query, doseq=True)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": "Bearer " + self.service_key, "Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            try:
                payload = self.transport(method, url, headers, encoded, self.timeout)
                if not isinstance(payload, dict):
                    raise HubSpotAPIError("HubSpot returned an invalid response")
                return payload
            except HTTPError as error:
                status = error.code
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    try:
                        delay = min(max(float(retry_after or 1), 0), 5)
                    except ValueError:
                        delay = 1
                    self.sleep(delay)
                    continue
                if status == 401:
                    raise HubSpotAPIError("HubSpot authentication failed; rotate or reload the Service Key", status) from None
                if status == 403:
                    raise HubSpotAPIError("HubSpot Service Key lacks a required CRM read permission", status) from None
                raise HubSpotAPIError(f"HubSpot API request failed with HTTP {status}", status) from None
            except (URLError, TimeoutError):
                if attempt < 2:
                    self.sleep(1)
                    continue
                raise HubSpotAPIError("HubSpot could not be reached") from None
        raise HubSpotAPIError("HubSpot request retry limit reached")

    def search(self, object_type, request_body):
        body = deepcopy(request_body)
        body.setdefault("limit", 100)
        records = []
        seen_cursors = set()
        while True:
            response = self.request("POST", f"/crm/objects/{self.api_version}/{object_type}/search", body=body)
            page = response.get("results", [])
            if not isinstance(page, list):
                raise HubSpotAPIError("HubSpot search results were invalid")
            records.extend(page)
            if len(records) > self.max_records:
                raise HubSpotAPIError("HubSpot search exceeded the configured record limit")
            after = response.get("paging", {}).get("next", {}).get("after")
            if after is None:
                return records
            if str(after) in seen_cursors:
                raise HubSpotAPIError("HubSpot returned a repeated paging cursor")
            seen_cursors.add(str(after))
            body["after"] = str(after)

    def get_object(self, object_type, object_id, properties):
        return self.request("GET", f"/crm/objects/{self.api_version}/{object_type}/{object_id}",
                            query={"properties": ",".join(properties)})

    def owner_name(self, owner_id):
        if not owner_id:
            return None
        owner_id = str(owner_id)
        if owner_id not in self._owners:
            payload = self.request("GET", f"/crm/owners/{self.api_version}/{owner_id}")
            name = " ".join(part for part in (payload.get("firstName"), payload.get("lastName")) if part)
            self._owners[owner_id] = name or payload.get("email") or owner_id
        return self._owners[owner_id]

    def stage_labels(self):
        if self._stages is None:
            payload = self.request("GET", f"/crm/pipelines/{self.api_version}/deals")
            self._stages = {(str(pipeline.get("id", "")), str(stage["id"])):
                            stage.get("label") or str(stage["id"])
                            for pipeline in payload.get("results", [])
                            for stage in pipeline.get("stages", []) if stage.get("id") is not None}
        return self._stages

    def check(self):
        self.search("companies", {"limit": 1, "properties": ["name"]})
        return {"service": "HubSpot CRM", "connected": True, "api_version": self.api_version,
                "max_records_per_tool_call": self.max_records}


class HubSpotIntegration:
    kind = "hubspot_crm"

    def __init__(self, config, client=None, environment=None):
        if not isinstance(config, dict):
            raise ValueError("HubSpot integration configuration must be an object")
        allowed = {"kind", "service_key_env", "api_version", "timeout_seconds", "max_records", "properties"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError("Unknown HubSpot configuration field: " + sorted(unknown)[0])
        env_name = config.get("service_key_env", "HUBSPOT_SERVICE_KEY")
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
            raise ValueError("HubSpot service_key_env must be an uppercase environment variable name")
        mapping = config.get("properties", {})
        if not isinstance(mapping, dict) or set(mapping) - set(PROPERTY_DEFAULTS):
            raise ValueError("HubSpot properties contains an unknown canonical field")
        self.properties = {**PROPERTY_DEFAULTS, **mapping}
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in self.properties.values()):
            raise ValueError("HubSpot property names must be nonempty strings or null")
        for required in ("company_name", "deal_closed"):
            if not self.properties[required]:
                raise ValueError(f"HubSpot {required} property cannot be null")
        environment = os.environ if environment is None else environment
        self.client = client or HubSpotClient(
            environment.get(env_name, ""), api_version=config.get("api_version", DEFAULT_API_VERSION),
            timeout=config.get("timeout_seconds", 15), max_records=config.get("max_records", 500))

    @staticmethod
    def _record_properties(record):
        properties = record.get("properties", {})
        if not isinstance(record.get("id"), (str, int)) or not isinstance(properties, dict):
            raise HubSpotAPIError("HubSpot returned a malformed CRM record")
        return properties

    def _requested(self, *keys):
        return list(dict.fromkeys(self.properties[key] for key in keys if self.properties[key]))

    @staticmethod
    def _date(value):
        if value in (None, ""):
            return None
        text = str(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
        try:
            if re.fullmatch(r"\d+", text):
                return datetime.fromtimestamp(int(text) / 1000, timezone.utc).date().isoformat()
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except (ValueError, OverflowError):
            raise HubSpotAPIError("HubSpot returned an invalid date property") from None

    @staticmethod
    def _number(value):
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HubSpotAPIError("HubSpot returned an invalid numeric property") from None
        if not math.isfinite(number):
            raise HubSpotAPIError("HubSpot returned a non-finite numeric property")
        return int(number) if number.is_integer() else number

    @staticmethod
    def _boolean(value):
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in ("true", "1", "yes")

    @staticmethod
    def _validate_ids(values, label):
        if not 1 <= len(values) <= 50:
            raise ToolCallError(f"Batch reads require between 1 and 50 {label} IDs")
        if len(set(values)) != len(values):
            raise ToolCallError(f"Batch {label} IDs must be unique")

    def _account(self, record):
        props = self._record_properties(record)
        identifier = str(record["id"])
        name = props.get(self.properties["company_name"]) or f"Unnamed HubSpot company {identifier}"
        owner_id = props.get(self.properties["company_owner"]) if self.properties["company_owner"] else None
        return {"customer_id": identifier, "company_name": name,
                "owner": self.client.owner_name(owner_id),
                "renewal_date": self._date(props.get(self.properties["renewal_date"])) if self.properties["renewal_date"] else None,
                "annual_value": self._number(props.get(self.properties["annual_value"])) if self.properties["annual_value"] else None,
                "support_customer_id": props.get(self.properties["support_customer_id"]) if self.properties["support_customer_id"] else None}

    def _contact(self, record, customer_id):
        props = self._record_properties(record)
        first = props.get(self.properties["contact_first_name"]) or ""
        last = props.get(self.properties["contact_last_name"]) or ""
        email = props.get(self.properties["contact_email"]) if self.properties["contact_email"] else None
        full_name = " ".join(part for part in (first, last) if part) or email or f"Unnamed HubSpot contact {record['id']}"
        return {"contact_id": str(record["id"]), "customer_id": customer_id, "full_name": full_name,
                "role": props.get(self.properties["contact_role"]) if self.properties["contact_role"] else None,
                "email": email,
                "is_primary": self._boolean(props.get(self.properties["contact_primary"])) if self.properties["contact_primary"] else False,
                "last_contacted_date": self._date(props.get(self.properties["contact_last_contacted"])) if self.properties["contact_last_contacted"] else None}

    def _deal(self, record, customer_id, stage_labels):
        props = self._record_properties(record)
        stage_id = str(props.get(self.properties["deal_stage"]) or "UNSPECIFIED")
        pipeline_id = str(props.get(self.properties["deal_pipeline"]) or "")
        owner_id = props.get(self.properties["deal_owner"]) if self.properties["deal_owner"] else None
        return {"deal_id": str(record["id"]), "customer_id": customer_id,
                "name": props.get(self.properties["deal_name"]) or f"Unnamed HubSpot deal {record['id']}",
                "amount": self._number(props.get(self.properties["deal_amount"])) if self.properties["deal_amount"] else None,
                "currency": props.get(self.properties["deal_currency"]) if self.properties["deal_currency"] else None,
                "stage": stage_labels.get((pipeline_id, stage_id), stage_labels.get(stage_id, stage_id)),
                "expected_close_date": self._date(props.get(self.properties["deal_close_date"])) if self.properties["deal_close_date"] else None,
                "owner": self.client.owner_name(owner_id),
                "updated_at": self._date(props.get(self.properties["deal_updated"])) if self.properties["deal_updated"] else None}

    def _company_properties(self):
        return self._requested("company_name", "company_owner", "renewal_date", "annual_value", "support_customer_id")

    def search_accounts(self, company_name):
        records = self.client.search("companies", {"query": company_name.strip(), "properties": self._company_properties()})
        rows = [self._account(record) for record in records]
        exact = [row for row in rows if row["company_name"].casefold() == company_name.strip().casefold()]
        rows = exact or rows
        return result(rows, [source("hubspot", "companies", row["customer_id"], row["company_name"]) for row in rows])

    def get_account(self, customer_id):
        try:
            record = self.client.get_object("companies", customer_id, self._company_properties())
        except HubSpotAPIError as error:
            if error.status == 404:
                raise ToolCallError("HubSpot company was not found") from error
            raise
        row = self._account(record)
        return result([row], [source("hubspot", "companies", row["customer_id"], row["company_name"])])

    def get_contacts(self, customer_id):
        filters = [{"propertyName": "associations.company", "operator": "EQ", "value": customer_id}]
        records = self.client.search("contacts", {"filterGroups": [{"filters": filters}],
                                                   "properties": self._requested("contact_first_name", "contact_last_name", "contact_role", "contact_email", "contact_primary", "contact_last_contacted")})
        rows = [self._contact(record, customer_id) for record in records]
        rows.sort(key=lambda row: (not row["is_primary"], row["contact_id"]))
        sources = [source("hubspot", "contact-search", customer_id, "HubSpot company contact search")]
        sources.extend(source("hubspot", "contacts", row["contact_id"], row["full_name"]) for row in rows)
        return result(rows, sources)

    def get_contacts_batch(self, customer_ids):
        self._validate_ids(customer_ids, "company")
        rows, sources = [], []
        for customer_id in customer_ids:
            response = self.get_contacts(customer_id)
            rows.extend(response["data"])
            sources.extend(response["sources"])
        return result(rows, sources)

    def get_deals(self, customer_id, stage):
        filters = [{"propertyName": "associations.company", "operator": "EQ", "value": customer_id}]
        if stage == "OPEN":
            filters.append({"propertyName": self.properties["deal_closed"], "operator": "EQ", "value": "false"})
        records = self.client.search("deals", {"filterGroups": [{"filters": filters}],
                                                "properties": self._requested("deal_name", "deal_amount", "deal_currency", "deal_stage", "deal_pipeline", "deal_close_date", "deal_owner", "deal_updated")})
        labels = self.client.stage_labels()
        rows = [self._deal(record, customer_id, labels) for record in records]
        sources = [source("hubspot", "deal-search", f"{customer_id}_{stage}", "HubSpot company deal search")]
        sources.extend(source("hubspot", "deals", row["deal_id"], row["name"]) for row in rows)
        return result(rows, sources)

    def get_deals_batch(self, customer_ids, stage):
        self._validate_ids(customer_ids, "company")
        rows, sources = [], []
        for customer_id in customer_ids:
            response = self.get_deals(customer_id, stage)
            rows.extend(response["data"])
            sources.extend(response["sources"])
        return result(rows, sources)

    def list_renewals(self, start_date, end_date):
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except (TypeError, ValueError):
            raise ToolCallError("Renewal dates must use valid YYYY-MM-DD values") from None
        if start > end:
            raise ToolCallError("Renewal start date must be on or before end date")
        property_name = self.properties["renewal_date"]
        if not property_name:
            raise ToolCallError("A HubSpot renewal-date property is not configured")
        to_millis = lambda value: str(int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp() * 1000))
        filters = [{"propertyName": property_name, "operator": "GTE", "value": to_millis(start)},
                   {"propertyName": property_name, "operator": "LTE", "value": to_millis(end)}]
        records = self.client.search("companies", {"filterGroups": [{"filters": filters}],
                                                    "properties": self._company_properties(),
                                                    "sorts": [property_name]})
        rows = [self._account(record) for record in records]
        sources = [source("hubspot", "renewals", f"{start_date}_{end_date}", "HubSpot renewal search")]
        sources.extend(source("hubspot", "companies", row["customer_id"], row["company_name"]) for row in rows)
        return result(rows, sources)

    def tools(self):
        account_ids = {"type": "array", "items": ACCOUNT_ID}
        tools = [
            Tool("crm_search_accounts", "Search live HubSpot companies by name. Returned fields may be unavailable when no HubSpot property mapping is configured.",
                 {"type": "object", "properties": {"company_name": {"type": "string", "minLength": 1, "pattern": r"\S"}},
                  "required": ["company_name"], "additionalProperties": False},
                 envelope_schema([ACCOUNT_SCHEMA]), self.search_accounts),
            Tool("crm_get_account", "Read a live HubSpot company by a known record ID.",
                 {"customer_id": "string"}, envelope_schema([ACCOUNT_SCHEMA]), self.get_account),
            Tool("crm_get_contacts", "List contacts associated with a known HubSpot company. Never infer a primary contact when no primary property is configured.",
                 {"customer_id": "string"}, envelope_schema([CONTACT_SCHEMA]), self.get_contacts),
            Tool("crm_get_contacts_batch", "List contacts for up to 50 known HubSpot companies. Use for multi-account workflows.",
                 {"type": "object", "properties": {"customer_ids": account_ids}, "required": ["customer_ids"], "additionalProperties": False},
                 envelope_schema([CONTACT_SCHEMA]), self.get_contacts_batch),
            Tool("crm_get_deals", "List ALL or OPEN deals associated with a known HubSpot company. OPEN uses HubSpot's hs_is_closed property.",
                 {"type": "object", "properties": {"customer_id": ACCOUNT_ID, "stage": {"type": "string", "enum": ["ALL", "OPEN"]}},
                  "required": ["customer_id", "stage"], "additionalProperties": False},
                 envelope_schema([DEAL_SCHEMA]), self.get_deals),
            Tool("crm_get_deals_batch", "List ALL or OPEN deals for up to 50 known HubSpot companies. Use for multi-account workflows.",
                 {"type": "object", "properties": {"customer_ids": account_ids, "stage": {"type": "string", "enum": ["ALL", "OPEN"]}},
                  "required": ["customer_ids", "stage"], "additionalProperties": False},
                 envelope_schema([DEAL_SCHEMA]), self.get_deals_batch),
        ]
        if self.properties["renewal_date"]:
            tools.append(Tool("crm_list_renewals", "Search live HubSpot companies in an inclusive renewal-date window.",
                              {"start_date": "string", "end_date": "string"},
                              envelope_schema([ACCOUNT_SCHEMA]), self.list_renewals))
        return tools

    def check_connection(self):
        return self.client.check()
