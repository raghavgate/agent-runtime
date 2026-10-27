from schema import json_schema, validate


class ToolCallError(ValueError):
    """An expected, correctable tool input or lookup problem."""


class ToolOutputError(RuntimeError):
    """An integration violated its declared output contract."""


class Tool:
    def __init__(self, name, description, inputSchema, outputSchema, function, read_only=True):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema
        self.outputSchema = outputSchema    
        self.function = function
        self.read_only = read_only
        self.input_schema = json_schema(inputSchema)
        self.output_schema = json_schema(outputSchema)

    def invoke(self, arguments):
        try:
            validate(arguments, self.input_schema)
        except ValueError as error:
            raise ToolCallError(str(error)) from error
        result = self.function(**arguments)
        try:
            validate(result, self.output_schema)
        except ValueError as error:
            raise ToolOutputError(str(error)) from error
        return result
       


