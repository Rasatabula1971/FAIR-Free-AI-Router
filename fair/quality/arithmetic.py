"""Bounded rational arithmetic without eval, executable code, or rounding."""

import ast
import re
from fractions import Fraction


def calculate(expression: str) -> Fraction:
    if not expression or len(expression) > 128 or not re.fullmatch(r"[0-9.()+*/\s-]+", expression):
        raise ValueError("Unsupported arithmetic expression")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, RecursionError) as error:
        raise ValueError("Invalid arithmetic expression") from error
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("Expression exceeds operation budget")

    def visit(node, depth=0):
        if depth > 16:
            raise ValueError("Expression exceeds depth budget")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            literal = ast.get_source_segment(expression.strip(), node)
            if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", literal):
                raise ValueError("Unsupported number")
            value = Fraction(literal)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth + 1) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div}:
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            try:
                match node.op:
                    case ast.Add():
                        value = left + right
                    case ast.Sub():
                        value = left - right
                    case ast.Mult():
                        value = left * right
                    case ast.Div():
                        value = left / right
            except ZeroDivisionError as error:
                raise ValueError("Division by zero") from error
        else:
            raise ValueError("Unsupported arithmetic operation")
        if value.numerator.bit_length() > 512 or value.denominator.bit_length() > 512:
            raise ValueError("Arithmetic exceeds size budget")
        return value

    return visit(tree.body)


def numeric_answer(text: str) -> Fraction:
    text = text.strip()
    if len(text) > 256 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text):
        raise ValueError("Expected only a decimal, integer, or fraction")
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Invalid numeric answer") from error
