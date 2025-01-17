from collections import OrderedDict

from copy import copy
from typpete.config import config
from typpete.constants import ALIASES, BUILTINS
from typpete.import_handler import ImportHandler
import ast


def get_module(node):
    if hasattr(node, "_module"):
        return node._module
    if hasattr(node, "_containing_class"):
        return get_module(node._containing_class)
    if hasattr(node, "_parent"):
        return get_module(node._parent)
    return None


class PreAnalyzer:
    """Analyzer for the AST, It provides the following configurations before the type inference:
    - The maximum args length of functions in the whole program
    - The maximum tuple length in the whole program
    - All the used names (variables, functions, classes, etc.) in the program,
        to be used in inferring relevant stub functions.
    - Class and instance attributes
    """

    def __init__(self, prog_ast, base_folder, stubs_handler):
        """
        :param prog_ast: The AST for the python program
        """
        # List all the nodes existing in the AST
        self.base_folder = base_folder
        self.analyzed = set()
        self.all_nodes = self.walk(prog_ast)

        # Pre-analyze only used constructs from the stub files.
        used_names = self.get_all_used_names()
        stub_asts = stubs_handler.get_relevant_ast_nodes(used_names)
        self.stub_nodes = []
        for stub_ast in stub_asts:
            self.stub_nodes += list(ast.walk(stub_ast))

    def walk(self, prog_ast):
        result = list(ast.walk(prog_ast))
        for n in result:
            n._module = prog_ast
        import_nodes = [node for node in result if isinstance(node, ast.Import)]
        import_from_nodes = [
            node for node in result if isinstance(node, ast.ImportFrom)
        ]
        for node in import_nodes:
            for name in node.names:
                if name in self.analyzed:
                    continue
                if ImportHandler.is_builtin(name.name):
                    new_ast = ImportHandler.get_builtin_ast(name.name)
                else:
                    new_ast = ImportHandler.get_module_ast(name.name, self.base_folder)
                self.analyzed.add(name)
                result += self.walk(new_ast)
        for node in import_from_nodes:
            if node.module == "typing":
                # FIXME ignore typing for now, not to break type vars
                continue
            if ImportHandler.is_builtin(node.module):
                new_ast = ImportHandler.get_builtin_ast(node.module)
            else:
                new_ast = ImportHandler.get_module_ast(node.module, self.base_folder)
            result += self.walk(new_ast)

        return result

    def add_stub_ast(self, tree):
        """Add an AST of a stub file to the pre-analyzer"""
        self.stub_nodes += list(ast.walk(tree))

    def maximum_function_args(self):
        """Get the maximum number of function arguments appearing in the AST"""
        user_func_defs = [
            node
            for node in self.all_nodes
            if isinstance(node, (ast.FunctionDef, ast.Lambda))
        ]
        stub_func_defs = [
            node
            for node in self.stub_nodes
            if isinstance(node, (ast.FunctionDef, ast.Lambda))
        ]

        # A minimum value of 1 because a default __init__ with one argument function
        # is added to classes that doesn't contain one
        user_func_max = max([len(node.args.args) for node in user_func_defs] + [1])
        stub_func_max = max([len(node.args.args) for node in stub_func_defs] + [1])

        return max(user_func_max, stub_func_max)

    def max_default_args(self):
        """Get the maximum number of default arguments appearing in all function definitions"""
        func_defs = [
            node
            for node in (self.all_nodes + self.stub_nodes)
            if isinstance(node, ast.FunctionDef)
        ]
        return max([len(node.args.defaults) for node in func_defs] + [0])

    def maximum_tuple_length(self):
        """Get the maximum length of tuples appearing in the AST"""
        user_tuples = [node for node in self.all_nodes if isinstance(node, ast.Tuple)]
        stub_tuples = [node for node in self.stub_nodes if isinstance(node, ast.Tuple)]

        user_max = max([len(node.elts) for node in user_tuples] + [0])
        stub_tuples = max([len(node.elts) for node in stub_tuples] + [0])

        return max(user_max, stub_tuples)

    def get_all_used_names(self):
        """Get all used variable names and used-defined classes names"""
        names = [node.id for node in self.all_nodes if isinstance(node, ast.Name)]
        names += [
            node.name for node in self.all_nodes if isinstance(node, ast.ClassDef)
        ]
        names += [
            node.attr for node in self.all_nodes if isinstance(node, ast.Attribute)
        ]
        names += [node.name for node in self.all_nodes if isinstance(node, ast.alias)]
        return names

    def analyze_functions(self, conf):
        """
        Pre-analyze functions
        """
        type_vars = [
            node
            for node in self.all_nodes + self.stub_nodes
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "TypeVar"
            )
        ]
        functions = [
            node
            for node in self.all_nodes + self.stub_nodes
            if isinstance(node, ast.FunctionDef)
        ]
        existing_type_vars = {
            str(item)
            for entry in dict(conf.type_params, **conf.class_type_params)
            for item in entry
        }

        for tv in type_vars:
            if not hasattr(tv, "_module"):
                continue
            tv_name = tv.targets[0].id
            tv_id = tv_name
            if tv_id in existing_type_vars:
                current = 0
                while tv_id + str(current) in existing_type_vars:
                    current += 1
                tv_id = tv_id + str(current)
            conf.type_vars[(tv._module, tv_name)] = tv_id

        for func in functions:
            local_type_vars = [
                name
                for (module, name) in conf.type_vars.keys()
                if module is get_module(func)
            ]
            all_tv_refs = set()
            for annotation in [
                ann.annotation for ann in func.args.args if ann.annotation is not None
            ] + ([func.returns] if func.returns else []):
                tv_refs = [
                    node.id
                    for node in ast.walk(annotation)
                    if isinstance(node, ast.Name) and node.id in local_type_vars
                ]
                tv_refs = list(OrderedDict.fromkeys(tv_refs))  # remove duplicates
                if hasattr(func, "_containing_class"):
                    class_tvs = {
                        node.id
                        for base in func._containing_class.bases
                        for node in ast.walk(base)
                        if isinstance(node, ast.Name) and node.id in local_type_vars
                    }
                    tv_refs = [tvr for tvr in tv_refs if tvr not in class_tvs]
                all_tv_refs |= set(tv_refs)
            if all_tv_refs:
                all_tv_refs = [conf.type_vars[(func._module, tv)] for tv in all_tv_refs]
                conf.type_params[func.name] = all_tv_refs

    def analyze_classes(self, conf):
        """Pre-analyze and configure classes before the type inference

        Do the following:
            - Propagate methods from bases to subclasses.
            - Add __init__ function to classes if it doesn't exist.
            - Return a mapping from class names to their attributes and methods.
            - Return a mapping from class names to their base classes if they have any.


        """
        class_defs = [
            node
            for node in self.all_nodes + self.stub_nodes
            if isinstance(node, ast.ClassDef)
        ]
        inherited_funcs_to_super = propagate_attributes_to_subclasses(class_defs)

        class_to_instance_attributes = OrderedDict()
        class_to_class_attributes = OrderedDict()
        class_to_base = OrderedDict()
        class_to_funcs = OrderedDict()
        abstract_classes = set()

        for cls in class_defs:
            key = cls.name
            if cls.bases:
                real_bases = [
                    b
                    for b in cls.bases
                    if not (isinstance(b, ast.Subscript) and b.value.id == "Generic")
                ]
                class_type_params = [b for b in cls.bases if b not in real_bases]
            else:
                real_bases = []
                class_type_params = []

            if class_type_params:
                if isinstance(class_type_params[0].slice.value, ast.Tuple):
                    args = [e.id for e in class_type_params[0].slice.value.elts]
                else:
                    args = [class_type_params[0].slice.value.id]
                conf.add_class_type_params(cls.name, args, get_module(cls))

            if cls.name in conf.class_type_params:
                key = (key,) + tuple(
                    [key + "_arg_" + str(n) for n in conf.class_type_params[key]]
                )

            if real_bases:
                class_to_base[key] = [x.id for x in cls.bases]
            else:
                class_to_base[key] = ["object"]

            kwords = cls.keywords
            has_abstract_method = False
            has_abcmeta = (
                kwords
                and kwords[0].arg == "metaclass"
                and (
                    isinstance(kwords[0].value, ast.Name)
                    and kwords[0].value.id == "ABCMeta"
                    or isinstance(kwords[0].value, ast.Attribute)
                    and kwords[0].value.attr == "ABCMeta"
                )
            )

            add_init_if_not_existing(cls)

            # instance attributes contains all attributes that can be accessed through the instance
            instance_attributes = set()

            # class attributes contains all attributes that can be accessed through the class
            # Ex:
            # class A:
            #     x = 1
            # A.x

            # class attributes are subset from instance attributes
            class_attributes = set()
            class_to_instance_attributes[cls.name] = instance_attributes
            class_to_class_attributes[cls.name] = class_attributes

            class_funcs = {}
            class_to_funcs[cls.name] = class_funcs

            # Inspect all class-level statements
            for cls_stmt in cls.body:
                if isinstance(cls_stmt, ast.FunctionDef):
                    cls_stmt._containing_class = cls
                    decorators = []
                    for d in cls_stmt.decorator_list:
                        decorator = d.id if isinstance(d, ast.Name) else d.attr
                        if decorator not in config["decorators"]:
                            raise TypeError(
                                "Decorator {} is not supported".format(decorator)
                            )
                        has_abstract_method |= decorator == "abstractmethod"
                        decorators.append(decorator)
                    class_funcs[cls_stmt.name] = (
                        len(cls_stmt.args.args),
                        decorators,
                        len(cls_stmt.args.defaults),
                    )
                    # Add function to class attributes and get attributes defined by self.some_attribute = value
                    instance_attributes.add(cls_stmt.name)
                    class_attributes.add(cls_stmt.name)
                    if not cls_stmt.args.args:
                        continue

                    if (
                        not config["allow_attributes_outside_init"]
                        and cls_stmt.name != "__init__"
                    ):
                        # Check if to allow attributes being defined outside __init__ or not
                        continue

                    first_arg = cls_stmt.args.args[
                        0
                    ].arg  # In most cases it will be 'self'

                    # Get attribute assignments where attribute value is the same as the first argument
                    func_nodes = ast.walk(cls_stmt)
                    assignments = [
                        node for node in func_nodes if isinstance(node, ast.Assign)
                    ]

                    for assignment in assignments:
                        for target in assignment.targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == first_arg
                            ):
                                instance_attributes.add(target.attr)

                elif isinstance(cls_stmt, ast.Assign):
                    # Get attributes defined as class-level assignment
                    for target in cls_stmt.targets:
                        if isinstance(target, ast.Name):
                            class_attributes.add(target.id)
                            instance_attributes.add(target.id)
            if has_abcmeta or has_abstract_method:
                abstract_classes.add(cls.name)
        return (
            class_to_instance_attributes,
            class_to_class_attributes,
            class_to_base,
            class_to_funcs,
            inherited_funcs_to_super,
            abstract_classes,
        )

    def get_all_configurations(self, class_type_params, type_params):
        config = Configuration(class_type_params, type_params)
        config.max_tuple_length = self.maximum_tuple_length()
        config.max_function_args = self.maximum_function_args()
        config.base_folder = self.base_folder

        class_analysis = self.analyze_classes(config)
        config.classes_to_instance_attrs = class_analysis[0]
        config.classes_to_class_attrs = class_analysis[1]
        config.class_to_base = class_analysis[2]
        config.class_to_funcs = class_analysis[3]
        config.inherited_funcs_to_super = class_analysis[4]
        config.abstract_classes = class_analysis[5]

        config.used_names = self.get_all_used_names()
        config.max_default_args = self.max_default_args()

        self.analyze_functions(config)

        config.update_type_vars()

        config.complete_class_to_base()

        return config


class Configuration:
    """A class holding configurations given by the pre-analyzer"""

    def __init__(self, class_type_params, type_params):
        self.max_tuple_length = 0
        self.max_function_args = 1
        self.classes_to_attrs = OrderedDict()
        self.class_to_base = OrderedDict()
        self.class_to_funcs = OrderedDict()
        self.base_folder = ""
        self.used_names = []
        self.max_default_args = 0
        self.all_classes = {}
        self.type_params = type_params
        self.class_type_params = class_type_params
        self.max_type_args = 0
        self.type_vars = OrderedDict()

    def update_type_vars(self):
        max_class_vars = 0
        max_func_vars = 0
        for vars in self.class_type_params.values():
            max_class_vars = max(max_class_vars, len(vars))
            for var in vars:
                self.type_vars[(None, var)] = str(var)
        for vars in self.type_params.values():
            max_func_vars = max(max_func_vars, len(vars))
            for var in vars:
                self.type_vars[(None, var)] = str(var)
        self.max_type_args = max_func_vars + max_class_vars

    def add_class_type_params(self, name, args, module):
        self.class_type_params[name] = args

    def complete_class_to_base(self):
        """
        Builds up the dictionary in self.all_classes, in which all classes (including
        builtins and classes from stubs) are mapped to their base classes.

        The dictionary refers to all classes by their Z3-level names. For generic classes,
        it contains tuples containing the Z3-level class name and subsequently the names
        of the accessor functions for the arguments.

        To be executed after max_tuple_length and max_function_args have been set.
        """
        builtins = dict(BUILTINS)

        for cur_len in range(self.max_tuple_length + 1):
            name = "tuple_" + str(cur_len)
            tuple_args = []
            for cur_arg in range(cur_len):
                arg_name = name + "_arg_" + str(cur_arg + 1)
                tuple_args.append(arg_name)
            if tuple_args:
                builtins[tuple([name] + tuple_args)] = ["tuple"]
            else:
                builtins[name] = ["tuple"]
        for cur_len in range(self.max_function_args + 1):
            name = "func_" + str(cur_len)
            func_args = [name + "_defaults_args"]
            for cur_arg in range(cur_len):
                arg_name = name + "_arg_" + str(cur_arg + 1)
                func_args.append(arg_name)
            func_args.append(name + "_return")
            builtins[tuple([name] + func_args)] = ["object"]
        self.all_classes = builtins
        for key, val in self.class_to_base.items():
            name = key if isinstance(key, str) else key[0]
            if name in ALIASES:
                continue
            ukey = "class_" + name
            if isinstance(key, tuple):
                ukey = (ukey,) + key[1:]
            uval = [x if x == "object" else "class_" + x for x in val]
            self.all_classes[ukey] = uval
        return


def get_non_empty_lists(lists):
    """

    :param lists: list of lists
    :return: list of lists of positive length
    """
    return [l for l in lists if l]


def merge(*lists):
    """Merge the lists according to C3 algorithm

    - Select first head of the lists which doesn't appear in the tail of any other list.
    - The selected element is removed from all the lists where it appears as a head and addead to the output list.
    - Repeat the above two steps until all the lists are empty
    - If no head can be removed and the lists are not yet empty, then no consistent MRO is possible.
    """
    res = []
    while True:
        lists = get_non_empty_lists(lists)  # Select only lists with positive length
        if not lists:
            # All lists are empty, then done.
            break
        removed_head = False
        for l in lists:
            head = l[0]
            head_can_be_removed = True
            # Check if the head doesn't appear in the tail of any list
            for l2 in lists:
                if head in l2[1:]:
                    head_can_be_removed = False
                    break
            if head_can_be_removed:
                # Can remove this head
                removed_head = True
                res.append(head)
                for l2 in lists:
                    if head in l2:
                        l2.remove(head)
                break

        if not removed_head:
            # Inconsistent MRO. Example:
            # class A(X, Y): ...
            # class B(Y, X): ...
            # class C(A, B): ...
            # C3 fails to resolve such structure
            raise TypeError("Cannot create a consistent method resolution order (MRO)")
    return res


def get_linearization(cls, class_to_bases):
    """Apply C3 linearization algorithm to resolve the MRO."""
    bases = class_to_bases[cls]
    bases_linearizations = [get_linearization(x, class_to_bases) for x in bases]
    return [cls] + merge(
        *bases_linearizations, copy(bases)
    )  # Copy `bases` so as not to modify the original mapping


def propagate_attributes_to_subclasses(class_defs):
    """Start depth-first methods propagation from inheritance roots to subclasses"""
    class_to_bases = {}
    class_to_node = {}
    class_inherited_funcs_to_super = {}
    for class_def in class_defs:
        class_to_bases[class_def.name] = [
            x.id
            for x in class_def.bases
            if isinstance(x, ast.Name) and x.id != "object"
        ]
        class_to_node[class_def.name] = class_def
        class_inherited_funcs_to_super[class_def.name] = {}

    # Save the inherited functions separately. Don't add them to the AST until
    # all classes are processed.
    class_to_inherited_funcs = {}
    class_to_inherited_attrs = {}
    for class_def in class_defs:
        class_linearization = get_linearization(class_def.name, class_to_bases)
        class_to_inherited_funcs[class_def.name] = []
        class_to_inherited_attrs[class_def.name] = []
        # Traverse the parents in the order given by MRO
        for parent in class_linearization:
            if parent == class_def.name:
                continue
            # Keep track of all added method names, so as not to add a duplicate method.
            class_funcs = {
                func.name
                for func in (class_def.body + class_to_inherited_funcs[class_def.name])
                if isinstance(func, ast.FunctionDef)
            }

            class_assignments = {
                stmt.targets[0].id
                for stmt in (class_def.body + class_to_inherited_attrs[class_def.name])
                if isinstance(stmt, ast.Assign)
            }

            parent_node = class_to_node[parent]
            # Select only functions that are not overridden in the subclasses.
            inherited_funcs = [
                func
                for func in parent_node.body
                if isinstance(func, ast.FunctionDef) and func.name not in class_funcs
            ]

            inherited_attrs = [
                stmt
                for stmt in parent_node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id not in class_assignments
            ]

            # Store a mapping from the inherited function names to the super class from which they are inherited
            for func in inherited_funcs:
                func.super = parent
            for attr in inherited_attrs:
                attr.super = parent
            class_inherited_funcs_to_super[class_def.name].update(
                {func.name: parent for func in inherited_funcs}
            )

            class_to_inherited_funcs[class_def.name] += inherited_funcs
            class_to_inherited_attrs[class_def.name] += inherited_attrs

    # Add the inherited functions to the AST.
    for class_def in class_defs:
        class_def.body += class_to_inherited_funcs[class_def.name]
        class_def.body += class_to_inherited_attrs[class_def.name]

    return class_inherited_funcs_to_super


def add_init_if_not_existing(class_node):
    """Add a default empty __init__ function if it doesn't exist in the class node"""
    for stmt in class_node.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__":
            return
    init_method = ast.FunctionDef(
        name="__init__",
        args=ast.arguments(
            args=[ast.arg(arg="self", annotation=None, lineno=class_node.lineno)],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=[ast.Pass()],
        decorator_list=[],
        returns=None,
        lineno=class_node.lineno,
    )
    init_method.remove_later = True
    class_node.body.append(init_method)
