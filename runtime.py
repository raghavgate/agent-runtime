class Runtime:
    def __init__(self, registry):
        self.registry = registry

    def execute(self, tool_name, arguments):
        selected_tool = self.registry.get(tool_name)
        return selected_tool.function(**arguments)
        
    def run(self, model, user_message):
        tool_calls = 0
        tool_result = None
        model_response = model.generate(user_message, tool_result)
        
        while model_response["type"] == "tool_call" and tool_calls < 10:
          tool_result = self.execute(model_response["tool_name"], model_response["arguments"])
          model_response = model.generate(user_message, tool_result)
          tool_calls += 1
          
        if model_response["type"] == "final_answer": 
          return model_response["content"]
        elif model_response["type"] == "tool_call":
          raise ValueError(f"model tool call limit: {tool_calls} tool calls reached")
        else:
          raise ValueError(f"Unexpected model response type: {model_response['type']}")
          
        
    

