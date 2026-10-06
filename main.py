from tool import Tool
from registry import ToolRegistry
from runtime import Runtime
from integrations.support import search_customer_tool, get_open_tickets_tool
from model import Fakemodel

def say_hello(name):
    return "hello" + name
    

def main():
    registry = ToolRegistry()
    registry.register(search_customer_tool)
    registry.register(get_open_tickets_tool)

    runtime = Runtime(registry)

    fakeModel = Fakemodel()

    model_response = fakeModel.generate ("Find Acme's latest open tickets")

    customer_result = (runtime.execute(model_response["tool_name"], model_response["arguments"]))

    second_response = fakeModel.generate("Find Acme's Latest Open Tickets", customer_result)
    
    tickets_result = (runtime.execute(second_response["tool_name"], second_response["arguments"]))

    print(registry.list_tools())
    print(model_response)
    print(second_response)

    print(tickets_result)
    
if __name__ == "__main__":
    main()