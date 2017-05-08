"""Inferrer for python statements.

Infers the types for the following expressions:
    - Assign(expr* targets, expr value)
    - AugAssign(expr target, operator op, expr value)
    - Return(expr? value)
    - Delete(expr* targets)
    - If(expr test, stmt* body, stmt* orelse)
    - While(expr test, stmt* body, stmt* orelse)
    - For(expr target, expr iter, stmt* body, stmt* orelse)
    - AsyncFor(expr target, expr iter, stmt* body, stmt* orelse)
    - With(withitem* items, stmt* body)
    - AsyncWith(withitem* items, stmt* body)
    - Try(stmt* body, excepthandler* handlers, stmt* orelse, stmt* finalbody)

    TODO:
    - Import(alias* names)
    - ImportFrom(identifier? module, alias* names, int? level)
    - Global(identifier* names)
    - Nonlocal(identifier* names)
"""

import ast
import frontend.expr_inferrer as expr
import frontend.z3_axioms as axioms
import frontend.z3_types as z3_types
import sys


def _infer_assignment_target(target, context, value):
    """Infer the type of a target in an assignment

    Attributes:
        target: the target whose type is to be inferred
        context: the current context level
        value: the value assigned to the target

    Target cases:
        - Variable name. Ex: x = 1
        - Tuple. Ex: a, b = 1, "string"
        - List. Ex: [a, b] = [1, "string"]
        - Subscript. Ex: x[0] = 1, x[1 : 2] = [2,3], x["key"] = value
        - Compound: Ex: a, b[0], [c, d], e["key"] = 1, 2.0, [True, False], "value"

    Limitation:
        - In case of tuple/list assignments, the value should be tuple/list too (not a variable name)
        For example, the following is not supported yet:
        x = (1, "string")
        a, b = x
        
    TODO: Attributes assignment
    """

    if isinstance(target, ast.Name):
        value_type = expr.infer(value, context)
        if context.has_variable(target.id):
            z3_types.solver.add(axioms.assignment(context.get_type(target.id), value_type))
        else:
            assignment_target_type = z3_types.new_z3_const("assign")
            z3_types.solver.add(axioms.assignment(assignment_target_type, value_type))
            context.set_type(target.id, assignment_target_type)
    elif isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (ast.Tuple, ast.List)):
            raise TypeError("Cannot unpack a non-sequence")
        if len(target.elts) != len(value.elts):
            raise ValueError("Cannot unpack {} values into {} targets".format(len(value.elts), len(target.elts)))
        for i in range(len(value.elts)):
            _infer_assignment_target(target.elts[i], context, value.elts[i])

    elif isinstance(target, ast.Subscript):
        value_type = expr.infer(value, context)
        indexed_type = expr.infer(target.value, context)
        if isinstance(target.slice, ast.Index):
            index_type = expr.infer(target.slice.value, context)
            z3_types.solver.add(axioms.index_assignment(indexed_type, index_type, value_type))
        else:  # Slice assignment
            lower_type = upper_type = step_type = z3_types.Int
            if target.slice.lower:
                lower_type = expr.infer(target.slice.lower, context)
            if target.slice.upper:
                upper_type = expr.infer(target.slice.upper, context)
            if target.slice.step:
                step_type = expr.infer(target.slice.step, context)
            z3_types.solver.add(axioms.slice_assignment(lower_type, upper_type, step_type, indexed_type, value_type))
    else:
        raise TypeError("The inference for {} assignment is not supported.".format(type(target).__name__))


def _infer_assign(node, context):
    """Infer the types of target variables in an assignment node."""

    for target in node.targets:
        _infer_assignment_target(target, context, node.value)

    return z3_types.zNone


def _infer_augmented_assign(node, context):
    """Infer the types for augmented assignments

    Examples:
        a += 5
        b[2] &= x

    TODO: Attribute augmented assignment
    """
    target_type = expr.infer(node.target, context)
    value_type = expr.infer(node.value, context)
    result_type = expr.binary_operation_type(target_type, node.op, value_type)

    if isinstance(node.target, ast.Name):
        z3_types.solver.add(axioms.assignment(target_type, result_type))
    elif isinstance(node.target, ast.Subscript):
        indexed_type = expr.infer(node.target.value, context)
        if isinstance(node.target.slice, ast.Index):
            index_type = expr.infer(node.target.slice.value, context)
            z3_types.solver.add(axioms.index_assignment(indexed_type, index_type, result_type))
        else:
            lower_type = upper_type = step_type = z3_types.Int
            if node.target.slice.lower:
                lower_type = expr.infer(node.target.slice.lower, context)
            if node.target.slice.upper:
                upper_type = expr.infer(node.target.slice.upper, context)
            if node.target.slice.step:
                step_type = expr.infer(node.target.slice.step, context)
            z3_types.solver.add(axioms.slice_assignment(lower_type, upper_type, step_type, indexed_type, result_type))

    elif isinstance(node.target, ast.Attribute):
        # TODO: Implement after classes inference
        pass
    return z3_types.zNone


def _delete_element(target, context):
    """Remove (if needed) a target from the context

    Cases:
        - del var_name: remove its type mapping from the context directly.
        - del subscript:
                    * Tuple/String --> Immutable. Raise exception.
                    * List/Dict --> Do nothing to the context.
    TODO: Attribute deletion
    """
    if isinstance(target, (ast.Tuple, ast.List)):  # Multiple deletions
        for elem in target.elts:
            _delete_element(elem, context)
    elif isinstance(target, ast.Name):
        context.delete_type(target.id)
    elif isinstance(target, ast.Subscript):
        expr.infer(target, context)
        indexed_type = expr.infer(target.value, context)
        z3_types.solver.add(axioms.delete_subscript(indexed_type))


def _infer_delete(node, context):
    """Remove (if needed) the type of the deleted items in the current context"""
    for target in node.targets:
        _delete_element(target, context)

    return z3_types.zNone


def _infer_body(body, context):
    """Infer the type of a code block containing multiple statements"""
    body_type = z3_types.new_z3_const("body")
    for stmt in body:
        stmt_type = infer(stmt, context)
        z3_types.solver.add(axioms.body(body_type, stmt_type))
    return body_type


def _infer_control_flow(node, context):
    """Infer the type(s) for an if/while/for statements block.

    Arguments:
        node: The AST node to be inferred
        context: the current context level
    Example:
        if (some_condition):
            ......
            return "some string"
        else:
            ......
            return 2.0

        type: Union{String, Float}
    """
    body_type = _infer_body(node.body, context)
    else_type = _infer_body(node.orelse, context)

    result_type = z3_types.new_z3_const("control_flow")

    z3_types.solver.add(axioms.control_flow(body_type, else_type, result_type))

    return result_type


def _infer_for(node, context):
    """Infer the type for a for loop node

    Limitation:
        - The iterable can't be a tuple.
            For example: the following is not allowed:
                for x in (1, 2.0, "string"):
                    ....
    """
    iter_type = expr.infer(node.iter, context)
    if not isinstance(iter_type, (TList, TSet, TIterator, TBytesString, TDictionary, TString)):
        raise TypeError("{} is not iterable.".format(iter_type))

    # Infer the target in the loop, inside the global context
    # Cases:
    # - Var name. Ex: for i in range(5)..
    # - Tuple. Ex: for (a,b) in [(1,"st"), (3,"st2")]..
    # - List. Ex: for [a,b] in [(1, "st"), (3, "st2")]..
    value_type = iter_type
    if isinstance(iter_type, (TList, TSet, TIterator)):
        value_type = value_type.type
    elif isinstance(iter_type, TDictionary):
        value_type = value_type.key_type
    _infer_assignment_target(node.target, context, value_type)

    return _infer_control_flow(node, context)


def _infer_with(node, context):
    """Infer the types for a with block"""
    for item in node.items:
        if item.optional_vars:
            item_type = expr.infer(item.context_expr, context)
            _infer_assignment_target(item.optional_vars, context, item_type)

    return _infer_body(node.body, context)


def _infer_try(node, context):
    """Infer the types for a try/except/else block"""
    try_type = UnionTypes()

    body_type = _infer_body(node.body, context)
    else_type = _infer_body(node.orelse, context)
    final_type = _infer_body(node.finalbody, context)
    if not isinstance(body_type, TNone):
        try_type.union(body_type)
    if not isinstance(else_type, TNone):
        try_type.union(else_type)
    if not isinstance(final_type, TNone):
        try_type.union(final_type)
    # TODO: Infer exception handlers as classes

    for handler in node.handlers:
        handler_body_type = _infer_body(handler.body, context)
        if not isinstance(handler_body_type, TNone):
            try_type.union(handler_body_type)

    if len(try_type.types) == 0:
        return TNone()
    elif len(try_type.types) == 1:
        return list(try_type.types)[0]
    return try_type


def infer(node, context):
    if isinstance(node, ast.Assign):
        return _infer_assign(node, context)
    elif isinstance(node, ast.AugAssign):
        return _infer_augmented_assign(node, context)
    elif isinstance(node, ast.Return):
        return expr.infer(node.value, context)
    elif isinstance(node, ast.Delete):
        return _infer_delete(node, context)
    elif isinstance(node, (ast.If, ast.While)):
        return _infer_control_flow(node, context)
    elif isinstance(node, ast.For):
        return _infer_for(node, context)
    elif sys.version_info[0] >= 3 and sys.version_info[1] >= 5 and isinstance(node, ast.AsyncFor):
        # AsyncFor is introduced in Python 3.5
        return _infer_for(node, context)
    elif isinstance(node, ast.With):
        return _infer_with(node, context)
    elif sys.version_info[0] >= 3 and sys.version_info[1] >= 5 and isinstance(node, ast.AsyncWith):
        # AsyncWith is introduced in Python 3.5
        return _infer_with(node, context)
    elif isinstance(node, ast.Try):
        return _infer_try(node, context)
    return z3_types.zNone
