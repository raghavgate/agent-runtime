class FakeModel:
    def generate(self, user_message, tool_result =None, tools = None):
        if tool_result is None:
            return({
                "type" : "tool_call",
                "tool_name" : "search_customer_tool",
                "arguments" : {"company_name" : "Acme"}
            })
        elif isinstance(tool_result, dict):
            return({
                "type" : "tool_call", 
                "tool_name" : "get_open_tickets_tool",
                "arguments" : {"customer_id" : tool_result["customer_id"]}
            })
        elif isinstance(tool_result, list):
            return({
                "type" : "final_answer",
                "content" : f"Acme has {len(tool_result)} open tickets"
                })
                
class ErrorModel:
    def generate(self, user_message, tool_result=None, tools =None):
        return {"type": "error"}
        
class LoopModel:
    def generate(self, user_message, tool_result = None, tools =None):
      return ({
      "type" : "tool_call",
      "tool_name" : "get_open_tickets_tool",
      "arguments" : {"customer_id" : 17}
      })
      
