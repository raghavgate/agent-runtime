class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        if tool.name in self.tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self.tools[tool.name] = tool

    def get(self, tool_name):
        if tool_name not in self.tools.keys():
          raise KeyError(f"Tool not found: {tool_name}")
        return self.tools[tool_name]
    
    def list_tools(self):
        tool_list = []
        for tools in self.tools.values():
            tool_info = {
                "name" : tools.name,
                "description" : tools.description,
                "inputSchema" : tools.input_schema,
                "outputSchema" : tools.output_schema,
                "read_only": tools.read_only,
            }
            tool_list.append(tool_info)

        return(tool_list)
