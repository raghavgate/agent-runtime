class Fakemodel:
    def generate(self, user_message, tool_result =None):
        if tool_result is None:
            return({
                "tool_name" : "search_customer_tool",
                "arguments" : {"company_name" : "Acme"}
            })
        else:
            return({
                "tool_name" : "get_open_tickets_tool",
                "arguments" : {"customer_id" : tool_result["customer_id"]}
            })