# HubSpot CRM connector

The HubSpot adapter gives the existing runtime live, read-only access to companies, contacts, and deals. It keeps HubSpot authentication, paging, property names, and API failures inside the integration layer; the runtime and model still see the same neutral CRM tools used by the local export adapter.

The connector makes no OpenAI requests by itself. Unit tests use a fake HubSpot client and make no network requests.

## 1. Create a free account and Service Key

For development, use a HubSpot developer test account or a non-production HubSpot account. HubSpot currently recommends **Service Keys** for new account-level, system-to-system data integrations. In HubSpot, open **Settings → Integrations → Service Keys** (or **Development → Keys → Service Keys**) and create a descriptively named key.

Grant only these read permissions:

- `crm.objects.companies.read`
- `crm.objects.contacts.read`
- `crm.objects.deals.read`
- the CRM owners read permission, so `hubspot_owner_id` can be shown as a person rather than an opaque ID

The adapter also reads deal pipeline metadata to turn stage IDs into labels. If HubSpot presents a separate pipeline-read permission in your account, grant that read permission too. Do not grant write permissions.

The creating HubSpot user needs Developer Tools access and can only grant permissions they already possess. Service Keys do not support webhooks, which is fine for this request/response prototype.

Official references:

- [HubSpot account types and test accounts](https://developers.hubspot.com/docs/getting-started/account-types)
- [Service Keys announcement and management path](https://developers.hubspot.com/changelog/service-keys)
- [HubSpot API versioning](https://developers.hubspot.com/docs/developer-tooling/platform/versioning)

## 2. Load the key without putting it in a file

In the terminal that will start the app:

```sh
cd ~/Desktop/agent-runtime
read -s "HUBSPOT_SERVICE_KEY?Paste HubSpot Service Key: "
export HUBSPOT_SERVICE_KEY
echo
```

The hidden `read` prevents the value from appearing on screen. The environment variable lasts for that terminal session. Never paste the key into JSON, Python, a prompt, a browser form, Git, or a screenshot. If it is exposed, rotate it in HubSpot.

Create a private working config from the committed example:

```sh
cp config.hubspot.example.json config.hubspot.local.json
```

`config.hubspot.local.json` is ignored by Git.

## 3. Verify the live connection

This command performs one explicit HubSpot read and makes zero model requests:

```sh
.venv/bin/python main.py --config config.hubspot.local.json check-connections
```

A successful result contains `"connected": true`. Common safe error messages are:

- `authentication failed`: the key is missing, expired, revoked, or was pasted into a different terminal.
- `lacks a required CRM read permission`: add the missing read scope; do not switch to broad write access.
- `could not be reached`: check the network and retry.

Provider response bodies and credentials are not included in raised errors.

## 4. Run the workspace

For the full language-model experience, the same terminal also needs `OPENAI_API_KEY` and `OPENAI_MODEL`:

```sh
.venv/bin/python main.py --config config.hubspot.local.json serve --provider openai --port 8768
```

Then open `http://127.0.0.1:8768`. Replace `Acme` in the suggested questions with a company that exists in the connected HubSpot account.

The demo model is intentionally not presented as supporting arbitrary live HubSpot questions. The connector itself is model-provider independent.

## Property mapping

The adapter uses these standard HubSpot properties automatically:

| Neutral field | HubSpot internal property |
| --- | --- |
| Company name | `name` |
| Company/contact/deal owner | `hubspot_owner_id` |
| Contact name | `firstname`, `lastname` |
| Contact role and email | `jobtitle`, `email` |
| Last contact date | `notes_last_contacted` |
| Deal name and value | `dealname`, `amount` |
| Deal currency | `deal_currency_code` |
| Deal stage and pipeline | `dealstage`, `pipeline` |
| Open/closed filtering | `hs_is_closed` |
| Expected close and update dates | `closedate`, `hs_lastmodifieddate` |

Renewal date, annual value, primary-contact designation, and an external support-system ID are business-specific. They default to `null`, which means **unavailable**, not empty or false. To enable them, put their HubSpot **internal property names** in `config.hubspot.local.json`:

```json
"properties": {
  "renewal_date": "contract_renewal_date",
  "annual_value": "annual_contract_value",
  "support_customer_id": "zendesk_organization_id",
  "contact_primary": "is_primary_contact"
}
```

The renewal tool is registered only when `renewal_date` is mapped. A contact is marked primary only when `contact_primary` is mapped and HubSpot returns a truthy value. Missing deal currency remains `null`; it is never assumed to be USD.

## Behavior and limits

- All HubSpot operations are `GET` or search `POST` requests. The search body describes filters; it does not mutate records.
- The adapter targets HubSpot's date-versioned `2026-09` APIs.
- Search results follow `paging.next.after` until complete.
- Each tool call stops if it would exceed `max_records` (500 in the example).
- HTTP 429 and transient 5xx responses retry at most twice, honoring a bounded `Retry-After` value.
- Batch tools accept 1–50 unique company IDs. Provider rate limits can still make a large batch slower.
- Contacts and deals are selected by their HubSpot company association, not by guessing matching IDs.
- `OPEN` deals use the provider's `hs_is_closed=false` field. Pipeline metadata converts internal stage IDs to human-readable labels.
- Saved traces and conversations can contain live CRM data. Their directories are local and ignored by Git.

For a live source, `dataset_hash` identifies the connector configuration, not an immutable snapshot of HubSpot. Re-running a question later may retrieve changed records. Gold evaluations therefore need a controlled test account or a captured fixture with independently authored expected results.

## Deliberate prototype boundary

This version is read-only and single-account. It does not create or update records, subscribe to webhooks, synchronize a local database, implement OAuth installation for multiple customers, or claim production access control. Those are separate product milestones, not hidden behavior in the connector.
