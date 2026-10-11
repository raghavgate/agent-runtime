from tool import Tool
from registry import ToolRegistry
from runtime import Runtime
from integrations.support import search_customer_tool, get_open_tickets_tool
from model import FakeModel, ErrorModel, LoopModel

def say_hello(name):
    return "hello" + name
    

def main():
    registry = ToolRegistry()
    registry.register(search_customer_tool)
    registry.register(get_open_tickets_tool)

    runtime = Runtime(registry)

    fakeModel = FakeModel()
    
    errorModel = ErrorModel()
    
    loopModel = LoopModel()
    
    user_message = "Find all open tickets for Acme"
        
    final_result = runtime.run(fakeModel, user_message)

    print(registry.list_tools())
    
    print(final_result)
    
if __name__ == "__main__":
    main()