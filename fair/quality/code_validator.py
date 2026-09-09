"""An interpreter for a small numeric Python subset; never exec/eval generated code."""

import ast
import operator

MAX_LIST = 64
MAX_STEPS = 4096
MAX_ITERATIONS = 1024
SAFE_BUILTINS = {
    name: value
    for name, value in (
        ("range", range),
        ("len", len),
        ("abs", abs),
        ("min", min),
        ("max", max),
        ("sum", sum),
        ("sorted", sorted),
    )
}


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
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.AugAssign,
    ast.List,
    ast.Subscript,
    ast.Call,
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
    if type(value) is list:
        if len(value) > MAX_LIST or any(type(item) not in {int, bool} for item in value):
            raise CodeRejected("CODE_VALUE_LIMIT")
        for item in value:
            bounded(item)
        return value
    if type(value) not in {int, bool} or (type(value) is int and value.bit_length() > 256):
        raise CodeRejected("CODE_VALUE_LIMIT")
    return value


def same_value(actual, expected):
    if type(actual) is not type(expected):
        return False
    if type(actual) is list:
        return len(actual) == len(expected) and all(
            same_value(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


def parse_function(source, name):
    if len(source) > 8192:
        raise CodeRejected("CODE_SIZE_LIMIT")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as error:
        raise CodeRejected("CODE_SYNTAX_FAILURE") from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 256 or any(type(node) not in ALLOWED for node in nodes):
        raise CodeRejected("CODE_SUBSET_VIOLATION")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise CodeRejected("CODE_FUNCTION_REQUIRED")
    function = tree.body[0]
    args = function.args
    if (
        function.name != name
        or function.name in SAFE_BUILTINS
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
        if isinstance(node, (ast.AugAssign, ast.For)) and not isinstance(node.target, ast.Name):
            raise CodeRejected("CODE_SUBSET_VIOLATION")
        if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            raise CodeRejected("CODE_SUBSET_VIOLATION")
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in SAFE_BUILTINS
        ) or (isinstance(node, ast.arg) and node.arg in SAFE_BUILTINS):
            raise CodeRejected("CODE_RESERVED_NAME")
        if isinstance(node, ast.Call):
            if (
                not isinstance(node.func, ast.Name)
                or node.func.id not in SAFE_BUILTINS
                or node.keywords
                or not 1 <= len(node.args) <= 8
            ):
                raise CodeRejected("CODE_SUBSET_VIOLATION")
            if node.func.id == "range" and not any(
                isinstance(parent, ast.For) and parent.iter is node for parent in nodes
            ):
                raise CodeRejected("CODE_SUBSET_VIOLATION")

    def loop_controls(body, loops=0):
        for statement in body:
            if isinstance(statement, (ast.Break, ast.Continue)) and not loops:
                raise CodeRejected("CODE_SUBSET_VIOLATION")
            if isinstance(statement, (ast.If, ast.For, ast.While)):
                loop_controls(statement.body, loops + isinstance(statement, (ast.For, ast.While)))
                loop_controls(statement.orelse, loops)

    loop_controls(function.body)
    return function


def run_case(function, arguments):
    if len(arguments) != len(function.args.args):
        raise CodeRejected("CODE_ARGUMENT_MISMATCH")
    scope = dict(
        zip(
            (arg.arg for arg in function.args.args),
            (bounded(value) for value in arguments),
            strict=True,
        )
    )
    budget = MAX_STEPS

    def step(depth):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 20:
            raise CodeRejected("CODE_OPERATION_LIMIT")

    def binary(op, left, right):
        if type(left) is list or type(right) is list:
            if isinstance(op, ast.Add) and type(left) is type(right) is list:
                if len(left) + len(right) > MAX_LIST:
                    raise CodeRejected("CODE_VALUE_LIMIT")
                return left + right
            raise CodeRejected("CODE_TYPE_FAILURE")
        return bounded(BINARY[type(op)](left, right))

    def call(node, depth):
        name = node.func.id
        args = [expression(arg, depth + 1) for arg in node.args]
        if name == "abs" and len(args) == 1 and type(args[0]) in {int, bool}:
            return abs(args[0])
        if name in {"len", "sorted", "sum"} and len(args) == 1 and type(args[0]) is list:
            if name == "sum":
                total = 0
                for item in args[0]:
                    step(depth)
                    total = bounded(total + item)
                return total
            return SAFE_BUILTINS[name](args[0])
        if name in {"min", "max"}:
            values = (
                args[0]
                if len(args) == 1 and type(args[0]) is list
                else args
                if len(args) >= 2
                else []
            )
            if values and all(type(item) in {int, bool} for item in values):
                return SAFE_BUILTINS[name](values)
        raise CodeRejected("CODE_TYPE_FAILURE")

    def expression(node, depth=0):
        step(depth)
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name) and node.id in scope:
            value = scope[node.id]
        elif isinstance(node, ast.List):
            if len(node.elts) > MAX_LIST:
                raise CodeRejected("CODE_VALUE_LIMIT")
            value = [expression(child, depth + 1) for child in node.elts]
        elif isinstance(node, ast.Subscript):
            sequence, index = expression(node.value, depth + 1), expression(node.slice, depth + 1)
            if type(sequence) is not list or type(index) not in {int, bool}:
                raise CodeRejected("CODE_TYPE_FAILURE")
            value = sequence[index]
        elif isinstance(node, ast.Call):
            value = call(node, depth)
        elif isinstance(node, ast.BinOp):
            value = binary(
                node.op, expression(node.left, depth + 1), expression(node.right, depth + 1)
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

    def iterable(node, depth):
        if isinstance(node, ast.Call) and node.func.id == "range":
            args = [expression(arg, depth + 1) for arg in node.args]
            if not 1 <= len(args) <= 3 or any(type(arg) not in {int, bool} for arg in args):
                raise CodeRejected("CODE_TYPE_FAILURE")
            values = range(*args)
            try:
                count = len(values)
            except OverflowError as error:
                raise CodeRejected("CODE_ITERATION_LIMIT") from error
            if count > MAX_ITERATIONS:
                raise CodeRejected("CODE_ITERATION_LIMIT")
            return values
        values = expression(node, depth + 1)
        if type(values) is not list:
            raise CodeRejected("CODE_TYPE_FAILURE")
        return values

    def statements(body, depth=0):
        for statement in body:
            step(depth)
            if isinstance(statement, ast.Return):
                return "return", expression(statement.value, depth + 1)
            if isinstance(statement, ast.Break):
                return "break", None
            if isinstance(statement, ast.Continue):
                return "continue", None
            if isinstance(statement, ast.Assign):
                scope[statement.targets[0].id] = expression(statement.value, depth + 1)
            elif isinstance(statement, ast.AugAssign):
                # Scalar-only augmented assignment avoids list alias mutation divergence.
                left = scope.get(statement.target.id)
                if type(left) not in {int, bool}:
                    raise CodeRejected("CODE_TYPE_FAILURE")
                scope[statement.target.id] = binary(
                    statement.op, left, expression(statement.value, depth + 1)
                )
            elif isinstance(statement, ast.If):
                branch = (
                    statement.body if expression(statement.test, depth + 1) else statement.orelse
                )
                control, value = statements(branch, depth + 1)
                if control:
                    return control, value
            elif isinstance(statement, (ast.For, ast.While)):
                items = (
                    iter(iterable(statement.iter, depth + 1))
                    if isinstance(statement, ast.For)
                    else None
                )
                broken = False
                count = 0
                while True:
                    step(depth + 1)
                    if items is not None:
                        try:
                            item = next(items)
                        except StopIteration:
                            break
                        scope[statement.target.id] = bounded(item)
                    elif not expression(statement.test, depth + 1):
                        break
                    count += 1
                    if count > MAX_ITERATIONS:
                        raise CodeRejected("CODE_ITERATION_LIMIT")
                    control, value = statements(statement.body, depth + 1)
                    if control == "return":
                        return control, value
                    if control == "break":
                        broken = True
                        break
                if not broken:
                    control, value = statements(statement.orelse, depth + 1)
                    if control:
                        return control, value
            else:
                raise CodeRejected("CODE_SUBSET_VIOLATION")
        return None, None

    try:
        control, value = statements(function.body)
        if control != "return":
            raise CodeRejected("CODE_MISSING_RETURN")
        return value
    except (ZeroDivisionError, IndexError, TypeError, ValueError, OverflowError) as error:
        if isinstance(error, CodeRejected):
            raise
        raise CodeRejected("CODE_RUNTIME_FAILURE") from error


def validate_function(source, contract):
    try:
        function = parse_function(source, contract.function_name)
        for case in contract.cases:
            actual = run_case(function, case.arguments)
            if not same_value(actual, case.expected):
                return False, "CODE_TEST_FAILURE"
    except CodeRejected as error:
        return False, str(error)
    return True, None
