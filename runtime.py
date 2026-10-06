class Runtime:
    def __init__(self, registry):
        self.registry = registry

    def execute(self, tool_name, arguments):
        selected_tool = self.registry.get(tool_name)
        return selected_tool.function(**arguments)
    

