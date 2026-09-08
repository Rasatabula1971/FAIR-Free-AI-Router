"""An interpreter for a small numeric Python subset; never exec/eval generated code."""

import ast
import operator


class CodeRejected(ValueError):
    pass


ALLOWED = {
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.If,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.FloorDiv,
    ast.Mod,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.IfExp,
}
BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def bounded(value):
    if type(value) not in {int, bool} or (type(value) is int and value.bit_length() > 256):
        raise CodeRejected("CODE_VALUE_LIMIT")
    return value


def parse_function(source, name):
    if len(source) > 8192:
        raise CodeRejected("CODE_SIZE_LIMIT")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as error:
        raise CodeRejected("CODE_SYNTAX_FAILURE") from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 128 or any(type(node) not in ALLOWED for node in nodes):
        raise CodeRejected("CODE_SUBSET_VIOLATION")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise CodeRejected("CODE_FUNCTION_REQUIRED")
    function = tree.body[0]
    args = function.args
    if (
        function.name != name
        or function.decorator_list
        or function.returns
        or function.type_params
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or len(args.args) > 8
        or any(arg.annotation for arg in args.args)
        or any(isinstance(node, ast.FunctionDef) and node is not function for node in nodes)
    ):
        raise CodeRejected("CODE_SIGNATURE_FAILURE")
    if len({arg.arg for arg in args.args}) != len(args.args):
        raise CodeRejected("CODE_SIGNATURE_FAILURE")
    # Validate unexecuted branches too, so dead code cannot hide unsupported operations.
    for node in nodes:
        if isinstance(node, ast.Constant):
            bounded(node.value)
        if isinstance(node, ast.Assign) and (
            len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
        ):
            raise CodeRejected("CODE_SUBSET_VIOLATION")
    return function


def run_case(function, arguments):
    if len(arguments) != len(function.args.args):
        raise CodeRejected("CODE_ARGUMENT_MISMATCH")
    scope = dict(zip((arg.arg for arg in function.args.args), arguments, strict=True))
    budget = 256

    def step(depth):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 20:
            raise CodeRejected("CODE_OPERATION_LIMIT")

    def expression(node, depth=0):
        step(depth)
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name) and node.id in scope:
            value = scope[node.id]
        elif isinstance(node, ast.BinOp):
            value = BINARY[type(node.op)](
                expression(node.left, depth + 1), expression(node.right, depth + 1)
            )
        elif isinstance(node, ast.UnaryOp):
            operand = expression(node.operand, depth + 1)
            value = (
                -operand
                if isinstance(node.op, ast.USub)
                else +operand
                if isinstance(node.op, ast.UAdd)
                else not operand
            )
        elif isinstance(node, ast.IfExp):
            branch = node.body if expression(node.test, depth + 1) else node.orelse
            value = expression(branch, depth + 1)
        elif isinstance(node, ast.BoolOp):
            for child in node.values:
                value = expression(child, depth + 1)
                if (isinstance(node.op, ast.And) and not value) or (
                    isinstance(node.op, ast.Or) and value
                ):
                    break
        elif isinstance(node, ast.Compare):
            left = expression(node.left, depth + 1)
            value = True
            for op, right_node in zip(node.ops, node.comparators, strict=True):
                right = expression(right_node, depth + 1)
                if not COMPARE[type(op)](left, right):
                    value = False
                    break
                left = right
        else:
            raise CodeRejected("CODE_UNBOUND_NAME")
        return bounded(value)

    def statements(body, depth=0):
        for statement in body:
            step(depth)
            if isinstance(statement, ast.Return):
                return True, expression(statement.value, depth + 1)
            if isinstance(statement, ast.Assign):
                scope[statement.targets[0].id] = expression(statement.value, depth + 1)
            elif isinstance(statement, ast.If):
                branch = (
                    statement.body if expression(statement.test, depth + 1) else statement.orelse
                )
                returned, value = statements(branch, depth + 1)
                if returned:
                    return True, value
            else:
                raise CodeRejected("CODE_SUBSET_VIOLATION")
        return False, None

    try:
        returned, value = statements(function.body)
        if not returned:
            raise CodeRejected("CODE_MISSING_RETURN")
        return value
    except ZeroDivisionError as error:
        raise CodeRejected("CODE_RUNTIME_FAILURE") from error


def validate_function(source, contract):
    try:
        function = parse_function(source, contract.function_name)
        for case in contract.cases:
            actual = run_case(function, case.arguments)
            if type(actual) is not type(case.expected) or actual != case.expected:
                return False, "CODE_TEST_FAILURE"
    except CodeRejected as error:
        return False, str(error)
    return True, None
