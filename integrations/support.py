from tool import Tool

customers = [
    {
        "customer_id" : 17,
        "company_name" : "Acme"
    },
    {
        "customer_id" : 23,
        "company_name" : "Globex"
    },
    {
        "customer_id" : 42,
        "company_name" : "Initech"
    }
]

tickets = [
    {
        "ticket_id" : 171,
        "customer_id" : 17,
        "status" : "OPEN",
        "priority" : "HIGH"
    },
    {
        "ticket_id" : 134,
        "customer_id" : 17,
        "status" : "OPEN",
        "priority" : "CRITICAL"
    },
    {
        "ticket_id" : 234,
        "customer_id" : 23,
        "status" : "OPEN",
        "priority" : "LOW"
    },
        {
        "ticket_id" : 652,
        "customer_id" : 17,
        "status" : "CLOSED",
        "priority" : "MEDIUM"
    }
]

def search_customer(company_name):
    for customer in customers:
        if customer["company_name"] == company_name:
            return customer
        
    return {"error" : "customer not found"}

search_customer_tool = Tool(
    "search_customer_tool",
    "searches for a customer based on company name", 
    {"company_name" : "string",},
    {
        "customer_id" : "integer",
        "company_name" : "string"
    },
    search_customer
    )


def get_open_tickets(customer_id):
    open_tickets = []

    for ticket in tickets:

        if ticket["status"] == "OPEN" and ticket["customer_id"] == customer_id:
            open_tickets.append(ticket)

    return open_tickets

get_open_tickets_tool = Tool(
    "get_open_tickets_tool",
    "finds open tickets for a company",
    {"customer_id" : "integer"},
    [
        {"ticket_id" : "integer",
            "customer_id" : "integer",
            "status" : "string",
            "priority" : "string"
            }
    ],
    get_open_tickets
)