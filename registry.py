class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool

    def get(self, tool_name):
        return self.tools[tool_name]
    
    def list_tools(self):
        tool_list = []
        for tools in self.tools.values():
            tool_info = {
                "name" : tools.name,
                "description" : tools.description,
                "inputSchema" : tools.inputSchema,
                "outputSchema" : tools.outputSchema
            }
            tool_list.append(tool_info)

        return(tool_list)