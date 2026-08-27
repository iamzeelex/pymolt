import ast
import re

def clean_code(line):
    # Remove leading/trailing whitespace and diff markers
    line = line.strip()
    if line.startswith('-') or line.startswith('+'):
        line = line[1:].strip()
    return line

def replace_subtrees(node, mapping):
    """
    Recursively replaces AST subtrees whose unparsed representation matches a key
    in mapping with a unified type-inferred placeholder (e.g. DataFrame_1, Series_2).
    """
    try:
        node_str = ast.unparse(node).strip()
        if node_str in mapping:
            return ast.Name(id=mapping[node_str], ctx=ast.Load())
    except Exception:
        pass

    # Recurse on child fields
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            new_list = []
            for item in value:
                if isinstance(item, ast.AST):
                    new_list.append(replace_subtrees(item, mapping))
                else:
                    new_list.append(item)
            setattr(node, field, new_list)
        elif isinstance(value, ast.AST):
            setattr(node, field, replace_subtrees(value, mapping))
            
    return node

def find_target_call(node, target_api):
    """
    Finds the first Call node in the AST calling the target attribute.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == target_api:
                return child
    return None

def infer_type(expr_str, target_api):
    """
    Heuristically infers the type of the expression (DataFrame, Series, Expr)
    based on the name and context.
    """
    expr_lower = expr_str.lower()
    if 'df' in expr_lower or 'frame' in expr_lower or 'result' in expr_lower or 'expected' in expr_lower or 'pad' in expr_lower:
        return 'DataFrame'
    if 'ser' in expr_lower or 'series' in expr_lower or 's' in expr_lower:
        return 'Series'
    if target_api in ('append', 'concat'):
        return 'DataFrame'
    return 'Expr'

def extract_patterns(diff_text, target_api):
    """
    Extracts structural type-inferred migration patterns from a diff hunk.
    """
    # Parse diff lines
    lines = diff_text.split('\n')
    minus_lines = [l for l in lines if l.startswith('-') and target_api in l]
    plus_lines = [l for l in lines if l.startswith('+')]
    
    if not minus_lines or not plus_lines:
        return None
        
    before_line = clean_code(minus_lines[0])
    after_line = clean_code(plus_lines[0])
    
    try:
        before_ast = ast.parse(before_line)
        after_ast = ast.parse(after_line)
        
        # Find the target API call to extract the sub-expressions (receiver and args)
        call_node = find_target_call(before_ast, target_api)
        if not call_node:
            return None
            
        # Build Prolog/Lisp-like type-inferred term unification mapping
        mapping = {}
        type_counts = {}
        ignore = {'True', 'False', 'None', 'self', 'pd', 'np', 'pytest', 'Series', 'DataFrame', 'concat', 'print'}
        
        def add_to_mapping(expr_node):
            expr_str = ast.unparse(expr_node).strip()
            if expr_str in ignore or expr_str in mapping:
                return
            tname = infer_type(expr_str, target_api)
            type_counts[tname] = type_counts.get(tname, 0) + 1
            mapping[expr_str] = f"{tname}_{type_counts[tname]}"

        # Map receiver (e.g. df, df[10:]) to Type_1
        add_to_mapping(call_node.func.value)
            
        # Map positional arguments to Type_2, Type_3...
        for arg in call_node.args:
            add_to_mapping(arg)
                    
        # Apply replacement recursively on both ASTs
        before_ast_replaced = replace_subtrees(before_ast, mapping)
        after_ast_replaced = replace_subtrees(after_ast, mapping)
        
        # Verify that at least one type-inferred placeholder is present in the replacement AST
        placeholders_in_after = set()
        for child in ast.walk(after_ast_replaced):
            if isinstance(child, ast.Name) and '_' in child.id:
                parts = child.id.split('_')
                if len(parts) == 2 and parts[1].isdigit():
                    placeholders_in_after.add(child.id)
                
        if not placeholders_in_after:
            return None
            
        pattern_before = ast.unparse(before_ast_replaced).strip()
        pattern_after = ast.unparse(after_ast_replaced).strip()
        
        # Remove identical assignment wrapper if present on both sides
        assign_match_b = re.match(r'^([\w_]+)\s*=\s*(.*)', pattern_before)
        assign_match_a = re.match(r'^([\w_]+)\s*=\s*(.*)', pattern_after)
        if assign_match_b and assign_match_a:
            if assign_match_b.group(1) == assign_match_a.group(1):
                pattern_before = assign_match_b.group(2)
                pattern_after = assign_match_a.group(2)
                
        return {
            "before": pattern_before,
            "after": pattern_after
        }
    except Exception:
        return None
