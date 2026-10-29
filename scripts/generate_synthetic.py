"""Reproducible fictional CRM/support snapshot. No network or model calls.

Evaluation answers are authored separately; this script never generates gold cases.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

AS_OF = '2026-10-28'
COMPANIES = [
    'Cedar & Finch', 'Northstar Labs', 'Northstar Retail', 'Bluewater Systems',
    'Orchard Supply', 'Meridian Health', 'Cobalt Works', 'Juniper Logistics',
    'Harborview Media', 'Atlas Fabrication', 'Willow Analytics', 'Pinecrest Foods',
    'Beacon Learning', 'Silverline Energy', 'Maple Street Retail', 'Stonebridge Legal',
    'Summit Field Services', 'Crescent Travel', 'Rivermark Software', 'Oakwell Packaging',
    'Eastgate Design', 'Lakeside Equipment', 'Copperleaf Research', 'Amberwell Services',
]
OWNERS = ['Maya Chen', 'Sam Rivera', 'Priya Shah']
PEOPLE = ['Avery Chen', 'Jordan Patel', 'Morgan Brooks', 'Riley Santos', 'Quinn Taylor', 'Casey Reed']


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def build_files():
    accounts, contacts, deals, tickets, organizations = [], [], [], [], []
    dates = ['2026-11-05', '2026-11-20', '2026-12-18', '2026-11-10', '2027-01-12', '2026-11-12', '2026-12-01', '2026-11-25']
    amounts = [72000, 36000, 28000, 24000, 12000, 54000, 18000, 84000]
    for index, company in enumerate(COMPANIES, 1):
        customer_id = f'acct-{index:03d}'
        organization = f'org-{1000 + index}' if index != 6 else ''
        renewal = dates[index - 1] if index <= 8 else f'2027-{(index % 8) + 1:02d}-{(index % 20) + 1:02d}'
        annual_value = amounts[index - 1] if index <= 8 else 12000 + index * 3000
        owner = OWNERS[(index - 1) % len(OWNERS)]
        account = {'customer_id': customer_id, 'company_name': company, 'owner': owner,
                   'renewal_date': renewal, 'annual_value': annual_value, 'support_customer_id': organization}
        accounts.append(account)
        slug = company.lower().replace(' & ', '-').replace(' ', '-')
        if index != 5:  # Known account with no contacts is deliberately retained.
            for offset in range(1, 4):
                name = PEOPLE[(index + offset - 2) % len(PEOPLE)]
                email = '.'.join(name.lower().split()) + '@' + slug + '.example'
                contacts.append({'contact_id': f'contact-{index:03d}-{offset}', 'customer_id': customer_id,
                                 'full_name': name, 'role': ['Operations Director', 'Finance Lead', 'IT Manager'][offset - 1],
                                 'email': None if index == 6 and offset == 1 else email,
                                 'is_primary': offset == 1 and index != 8,
                                 'last_contacted_date': None if offset == 3 else f'2026-10-{24 - offset:02d}'})
        for offset in (1, 2):
            stage = ['NEGOTIATION', 'PROPOSAL'][offset - 1]
            if index == 7:
                stage = ['CLOSED_WON', 'CLOSED_LOST'][offset - 1]
            elif index > 8 and index % 4 == 0:
                stage = ['CLOSED_WON', 'QUALIFICATION'][offset - 1]
            deals.append({'deal_id': f'deal-{index:03d}-{offset}', 'customer_id': customer_id,
                          'name': company + (' Renewal' if offset == 1 else ' Expansion'),
                          'amount': annual_value if offset == 1 else annual_value // 4,
                          'currency': 'USD', 'stage': stage, 'expected_close_date': renewal if offset == 1 else '2026-12-15',
                          'owner': owner, 'updated_at': '2026-10-20'})
        if organization:
            organizations.append(organization)
        if index in (4, 6):  # Empty ticket result versus unavailable mapping.
            continue
        for offset in range(1, 5):
            status = ['OPEN', 'OPEN', 'CLOSED', 'PENDING'][offset - 1]
            if index == 7:
                status = 'CLOSED'
            priority = ['CRITICAL' if index == 1 else 'LOW', 'HIGH' if index == 1 else 'MEDIUM', 'CRITICAL', 'MEDIUM'][offset - 1]
            subject = ['Invoice export blocked', 'Account sync delays', 'Resolved login outage', 'Awaiting customer logs'][offset - 1]
            tickets.append({'ticket_id': 10000 + index * 10 + offset, 'organization_id': organization,
                            'status': status, 'priority': priority, 'subject': subject})
    crm = {'accounts': accounts, 'contacts': contacts, 'deals': deals}
    support = {'organizations': organizations, 'tickets': tickets}
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(accounts[0]), lineterminator='\n')
    writer.writeheader()
    writer.writerows(accounts)
    files = {'crm.json': encoded(crm), 'support.json': encoded(support), 'accounts.csv': stream.getvalue().encode()}
    manifest = {'name': 'Synthetic CRM sandbox', 'data_origin': 'synthetic', 'as_of': AS_OF,
                'currency': 'USD', 'version': 1,
                'counts': {'accounts': len(accounts), 'contacts': len(contacts), 'deals': len(deals),
                           'support_organizations': len(organizations), 'tickets': len(tickets)},
                'files_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
                'edge_cases': ['Two Northstar accounts require clarification', 'Bluewater has a support organization but no tickets',
                               'Orchard has no contacts', 'Meridian has no support mapping and a primary contact without email',
                               'Juniper has contacts but no designated primary', 'Cobalt has only closed deals and closed tickets',
                               'Closed critical and pending tickets must not be counted as OPEN'],
                'notice': 'All organizations, people and records are fictional examples. Email addresses use the reserved .example domain. This snapshot is not evidence of performance on real CRM data.'}
    files['manifest.json'] = encoded(manifest)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'data' / 'synthetic')
    parser.add_argument('--check', action='store_true', help='Verify checked-in files without modifying them')
    args = parser.parse_args()
    files = build_files()
    if args.check:
        mismatches = [name for name, data in files.items() if not (args.output / name).exists() or (args.output / name).read_bytes() != data]
        if mismatches:
            raise SystemExit('Snapshot differs: ' + ', '.join(mismatches))
        print('Synthetic snapshot matches the generator.')
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (args.output / name).write_bytes(data)
        print(json.dumps(json.loads(files['manifest.json'])['counts']))


if __name__ == '__main__':
    main()
