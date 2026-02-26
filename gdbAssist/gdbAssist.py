#!/usr/bin/env python3
"""
gdbAssist.py - Map GDB x/nx memory dump to C/C++ struct fields

Usage:
    python3 gdbAssist.py --type TYPE_NAME --gdb GDB_OUTPUT_FILE
                         [--build-dir BUILD_DIR] [--bits 32|64]
                         [--src-dirs DIR1,DIR2,...] [--output OUTPUT_FILE]

Input:  GDB output file (from x/nx command)
Output: Structured field mapping text file
"""

import re
import os
import sys
import argparse
import subprocess
import json
from collections import OrderedDict


# ---------------------------------------------------------------------------
# Basic type table:  name -> (size_32bit, size_64bit, align_32bit, align_64bit)
# ---------------------------------------------------------------------------
BASIC_TYPES = {
    # C99 fixed-width
    'uint8_t':   (1, 1, 1, 1),
    'uint16_t':  (2, 2, 2, 2),
    'uint32_t':  (4, 4, 4, 4),
    'uint64_t':  (8, 8, 8, 8),
    'int8_t':    (1, 1, 1, 1),
    'int16_t':   (2, 2, 2, 2),
    'int32_t':   (4, 4, 4, 4),
    'int64_t':   (8, 8, 8, 8),
    'u8':        (1, 1, 1, 1),
    'u16':       (2, 2, 2, 2),
    'u32':       (4, 4, 4, 4),
    'u64':       (8, 8, 8, 8),
    's8':        (1, 1, 1, 1),
    's16':       (2, 2, 2, 2),
    's32':       (4, 4, 4, 4),
    's64':       (8, 8, 8, 8),
    # C primitives
    'char':      (1, 1, 1, 1),
    'uchar':     (1, 1, 1, 1),
    'unsigned char':  (1, 1, 1, 1),
    'signed char':    (1, 1, 1, 1),
    'short':     (2, 2, 2, 2),
    'unsigned short': (2, 2, 2, 2),
    'int':       (4, 4, 4, 4),
    'unsigned int':   (4, 4, 4, 4),
    'unsigned':  (4, 4, 4, 4),
    'long':      (4, 8, 4, 8),
    'unsigned long':  (4, 8, 4, 8),
    'long long':      (8, 8, 8, 8),
    'unsigned long long': (8, 8, 8, 8),
    'float':     (4, 4, 4, 4),
    'double':    (8, 8, 8, 8),
    'long double': (12, 16, 4, 16),
    'bool':      (1, 1, 1, 1),
    '_Bool':     (1, 1, 1, 1),
    'size_t':    (4, 8, 4, 8),
    'ssize_t':   (4, 8, 4, 8),
    'ptrdiff_t': (4, 8, 4, 8),
    'intptr_t':  (4, 8, 4, 8),
    'uintptr_t': (4, 8, 4, 8),
    'void':      (0, 0, 1, 1),
}


def get_basic_size_align(type_name: str, bits: int) -> tuple:
    """Return (size, align) for a basic type; bits is 32 or 64."""
    idx_size = 0 if bits == 32 else 1
    idx_align = 2 if bits == 32 else 3
    row = BASIC_TYPES.get(type_name)
    if row is None:
        return None
    return row[idx_size], row[idx_align]


def align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        return value
    return ((value + alignment - 1) // alignment) * alignment


# ---------------------------------------------------------------------------
# C/C++ type parser
# ---------------------------------------------------------------------------

class Field:
    """Represents one field in a struct/class."""
    def __init__(self, name: str, base_type: str, is_pointer: bool,
                 pointer_depth: int, array_dims: list):
        self.name = name
        self.base_type = base_type      # stripped type name (no * or [])
        self.is_pointer = is_pointer    # True if any pointer level
        self.pointer_depth = pointer_depth  # number of * levels
        self.array_dims = array_dims    # e.g. [2, 4] for [2][4]

    def __repr__(self):
        dims = ''.join(f'[{d}]' for d in self.array_dims)
        ptr = '*' * self.pointer_depth
        return f"Field({self.base_type}{ptr} {self.name}{dims})"


class StructDef:
    """Represents a parsed struct or class definition."""
    def __init__(self, name: str, fields: list, is_class: bool = False):
        self.name = name
        self.fields = fields    # list of Field
        self.is_class = is_class

    def __repr__(self):
        return f"StructDef({self.name}, fields={self.fields})"


def strip_comments(code: str) -> str:
    """Remove C/C++ comments from source code."""
    # Remove block comments
    code = re.sub(r'/\*.*?\*/', ' ', code, flags=re.DOTALL)
    # Remove line comments
    code = re.sub(r'//[^\n]*', ' ', code)
    return code


def strip_preprocessor(code: str) -> str:
    """Remove preprocessor directives (but keep content for simplicity)."""
    # Remove #include, #define simple macros, #pragma, #if blocks naively
    # We keep the text but remove # lines to avoid confusion
    result = []
    for line in code.split('\n'):
        stripped = line.strip()
        if stripped.startswith('#'):
            result.append('')   # blank out, preserve line count
        else:
            result.append(line)
    return '\n'.join(result)


def find_matching_brace(code: str, start: int) -> int:
    """Find the position of the closing '}' matching the '{' at start."""
    depth = 0
    i = start
    while i < len(code):
        if code[i] == '{':
            depth += 1
        elif code[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def normalize_type(type_str: str) -> str:
    """Normalize whitespace in type string."""
    # collapse multiple spaces
    s = re.sub(r'\s+', ' ', type_str.strip())
    # normalize "unsigned int" etc.
    return s


def parse_field_declaration(decl: str) -> list:
    """
    Parse a single field declaration (without trailing semicolon).
    Returns list of Field objects (a declaration can declare multiple names).
    e.g. "uint32_t a, b" -> [Field('a', 'uint32_t'), Field('b', 'uint32_t')]
    e.g. "uint32_t *ptr" -> [Field('ptr', 'uint32_t', pointer_depth=1)]
    e.g. "uint32_t arr[2]" -> [Field('arr', 'uint32_t', array_dims=[2])]
    """
    decl = decl.strip()
    if not decl:
        return []

    # Skip access specifiers
    if decl in ('public', 'private', 'protected'):
        return []

    fields = []

    # Split on comma to handle "int a, b, c"
    # But be careful: commas inside [] should not split
    # For simplicity, we handle one name at a time (most structs do one per line)
    # First, find the base type vs declarator(s)

    # Strategy: find last token(s) that are names/pointers/arrays
    # The type is everything before the last declarator group

    # Tokenize roughly
    # Remove any inline struct/class (shouldn't appear in field decl after body extraction)

    # Find all declarators (name + optional array/pointer modifiers)
    # Type is everything before the first declarator

    # Simple approach: split on comma, parse each name-part
    parts = split_declarators(decl)

    for (base_type, name, pointer_depth, array_dims) in parts:
        base_type = normalize_type(base_type)
        if not name:
            continue
        is_pointer = pointer_depth > 0
        fields.append(Field(name, base_type, is_pointer, pointer_depth, array_dims))

    return fields


def split_declarators(decl: str):
    """
    Split a declaration like "uint32_t *a, b[2], **c" into:
    [('uint32_t', 'a', 1, []),
     ('uint32_t', 'b', 0, [2]),
     ('uint32_t', 'c', 2, [])]

    Returns list of (base_type, name, pointer_depth, array_dims).
    """
    decl = decl.strip()

    # Find the split point between the base type and first declarator
    # The base type is the longest prefix that is a valid type keyword sequence
    # We do this by scanning tokens

    tokens = re.split(r'(\*+|,|\[|\]|\w+)', decl)
    tokens = [t for t in tokens if t.strip()]

    # Collect base type tokens (keywords that can be types)
    TYPE_KEYWORDS = {
        'unsigned', 'signed', 'long', 'short', 'const', 'volatile',
        'struct', 'class', 'union', 'enum', 'inline', 'extern', 'static',
        'register', 'auto', 'restrict', '__restrict',
    }
    # Also any alphanumeric token ending with _t or known type pattern

    results = []

    # We'll use a regex approach: match the whole declaration
    # Pattern: [type-qualifiers] base_type [* ...] name [array-dims] [, [* ...] name [array-dims] ...]

    # Remove 'const', 'volatile', 'restrict' qualifiers from the front
    qualifier_re = re.compile(r'^(const|volatile|restrict|__restrict|static|extern|inline|register)\s+')
    while qualifier_re.match(decl):
        decl = qualifier_re.sub('', decl)

    # Match: base_type = one or more words (possibly "unsigned int", "long long", etc.)
    # followed by one or more declarators separated by commas
    #
    # We look for the last "word-like token before the first * or name-declarator"

    # Find the base type: everything up to the first declarator
    # A declarator starts with: * or an identifier that is followed by [ or , or end
    # The base type can be: "unsigned int", "struct Foo", "uint32_t", etc.

    # Strategy: find where the type ends and the name starts
    # Type ends when we see a * or when we see an identifier that is followed by
    # [, ,, end-of-string, or another identifier (means the first was the type, second is name)

    # Use regex to match the whole thing
    # Group 1: base type (words, possibly qualified)
    # Group 2+: declarator list

    # Match like: TYPE_PART DECLARATOR (, DECLARATOR)*
    # where DECLARATOR is: *... NAME [DIMS...]

    # Regex for base type (greedy words)
    m = re.match(
        r'^((?:(?:unsigned|signed|long|short|const|volatile|struct|union|enum|class)\s+)*\w[\w:]*)'
        r'\s*(.*)',
        decl
    )
    if not m:
        return []

    base_type = m.group(1).strip()
    rest = m.group(2).strip()

    # Handle "long long", "unsigned long long", etc.
    # If base_type ends with 'long' or 'unsigned' etc. and rest starts with 'long' or 'int'
    long_extend = re.match(r'^(long|int)\b', rest)
    if long_extend and base_type.split()[-1] in ('long', 'unsigned', 'signed'):
        base_type = base_type + ' ' + long_extend.group(1)
        rest = rest[long_extend.end():].strip()

    # Now parse declarator list: "* name [dims], ** name2 [dims2], ..."
    # Split on top-level commas
    decl_parts = split_top_level_comma(rest)

    for dp in decl_parts:
        dp = dp.strip()
        if not dp:
            continue
        # Count leading *
        ptr_m = re.match(r'^(\*+)\s*(.*)', dp)
        if ptr_m:
            pointer_depth = len(ptr_m.group(1))
            dp = ptr_m.group(2).strip()
        else:
            pointer_depth = 0

        # Now dp should be: name [dims...]
        name_m = re.match(r'^(\w+)(.*)', dp)
        if not name_m:
            continue
        name = name_m.group(1)
        dims_str = name_m.group(2).strip()

        array_dims = []
        for dim_m in re.finditer(r'\[(\d+)\]', dims_str):
            array_dims.append(int(dim_m.group(1)))

        results.append((base_type, name, pointer_depth, array_dims))

    return results


def split_top_level_comma(s: str) -> list:
    """Split string on commas that are not inside brackets."""
    parts = []
    depth = 0
    current = []
    for ch in s:
        if ch in '([{':
            depth += 1
            current.append(ch)
        elif ch in ')]}':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts


def extract_struct_body(code: str, start: int) -> tuple:
    """
    Given code with a '{' at position start (or after it), extract the body.
    Returns (body_str, end_pos) where end_pos is the index after '}'.
    """
    brace_pos = code.index('{', start)
    end_pos = find_matching_brace(code, brace_pos)
    if end_pos == -1:
        return None, -1
    body = code[brace_pos+1:end_pos]
    return body, end_pos


def parse_struct_body(body: str, type_registry: dict) -> list:
    """
    Parse the fields from a struct/class body.
    Returns list of Field objects.
    Handles nested anonymous structs/unions (flatten) and named nested structs.
    """
    fields = []
    # We need to handle nested struct/class/union definitions inline
    # Strategy: process line by line, but handle {} blocks

    # Remove nested struct/class/union definitions (they are registered separately)
    # and replace with their type name for field declarations

    body = body.strip()
    pos = 0
    while pos < len(body):
        # Skip whitespace
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break

        # Check for access specifier
        acc_m = re.match(r'(public|private|protected)\s*:', body[pos:])
        if acc_m:
            pos += acc_m.end()
            continue

        # Check for nested struct/class/union definition
        nested_m = re.match(r'(struct|class|union)\s*(\w*)\s*\{', body[pos:])
        if nested_m:
            kw = nested_m.group(1)
            nested_name = nested_m.group(2)
            # Find the matching brace
            brace_start = pos + nested_m.start(0) + nested_m.end(0) - 1
            # Actually find { in the matched part
            brace_idx = body.index('{', pos + nested_m.start(0))
            end_brace = find_matching_brace(body, brace_idx)
            if end_brace == -1:
                pos += nested_m.end()
                continue
            nested_body = body[brace_idx+1:end_brace]

            # After the closing brace, there may be: " var_name;" or ";"
            after = body[end_brace+1:].lstrip()
            var_m = re.match(r'(\w+)\s*(\[[^\]]*\])?\s*;', after)

            if nested_name:
                # Parse nested type and register it
                nested_fields = parse_struct_body(nested_body, type_registry)
                sd = StructDef(nested_name, nested_fields, is_class=(kw == 'class'))
                type_registry[nested_name] = sd

            if var_m:
                # This is "struct/class Name nested_name { ... } var_name;"
                var_name = var_m.group(1)
                array_part = var_m.group(2) or ''
                array_dims = [int(x) for x in re.findall(r'\[(\d+)\]', array_part)]
                type_name = nested_name if nested_name else f'_anon_{id(nested_body)}'
                if not nested_name:
                    # Register anonymous struct
                    nested_fields_anon = parse_struct_body(nested_body, type_registry)
                    sd_anon = StructDef(type_name, nested_fields_anon)
                    type_registry[type_name] = sd_anon
                fields.append(Field(var_name, type_name, False, 0, array_dims))
                pos = end_brace + 1 + var_m.end()
            else:
                # Anonymous struct/union without variable name - flatten fields
                if not nested_name:
                    anon_fields = parse_struct_body(nested_body, type_registry)
                    fields.extend(anon_fields)
                pos = end_brace + 1
                # skip the semicolon after
                after2 = body[pos:].lstrip()
                if after2.startswith(';'):
                    pos += body[pos:].index(';') + 1

            continue

        # Regular field declaration: find the semicolon
        semi_pos = body.find(';', pos)
        if semi_pos == -1:
            break
        decl = body[pos:semi_pos].strip()
        pos = semi_pos + 1

        if not decl:
            continue

        # Skip friend declarations, using, typedef inside body etc.
        if re.match(r'(friend|using|typedef|virtual|explicit|override|final)\b', decl):
            continue

        # Skip constructor/destructor/method declarations (contain parentheses with parameters)
        if '(' in decl:
            continue

        parsed = parse_field_declaration(decl)
        fields.extend(parsed)

    return fields


class TypeParser:
    """Parses C/C++ source files and builds a type registry."""

    def __init__(self):
        self.type_registry = {}   # name -> StructDef or str (alias)
        # Pre-populate with basic type aliases
        for t in BASIC_TYPES:
            self.type_registry[t] = t  # basic types map to themselves

    def parse_file(self, filepath: str):
        """Parse a single source/header file."""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                code = f.read()
        except OSError:
            return

        code = strip_comments(code)
        code = strip_preprocessor(code)
        self._parse_code(code)

    def _parse_code(self, code: str):
        """Extract struct/class/typedef definitions from code."""
        pos = 0
        while pos < len(code):
            # Try to match struct/class/union definition
            m = re.match(
                r'\b(struct|class|union)\s+(\w+)\s*(?::[^{]*)?\{',
                code[pos:]
            )
            if m:
                abs_start = pos + m.start()
                name = m.group(2)
                kw = m.group(1)
                # Find body
                brace_idx = code.index('{', pos + m.start())
                end_brace = find_matching_brace(code, brace_idx)
                if end_brace == -1:
                    pos += m.end()
                    continue
                body = code[brace_idx+1:end_brace]
                nested_reg = dict(self.type_registry)
                fields = parse_struct_body(body, nested_reg)
                self.type_registry.update(nested_reg)
                sd = StructDef(name, fields, is_class=(kw == 'class'))
                self.type_registry[name] = sd
                pos = end_brace + 1
                continue

            # Try to match typedef struct { ... } Name;
            m2 = re.match(
                r'\btypedef\s+(struct|class|union)\s*(\w*)\s*\{',
                code[pos:]
            )
            if m2:
                kw = m2.group(1)
                tag_name = m2.group(2)
                brace_idx = code.index('{', pos + m2.start())
                end_brace = find_matching_brace(code, brace_idx)
                if end_brace == -1:
                    pos += m2.end()
                    continue
                body = code[brace_idx+1:end_brace]
                # Find the typedef name after closing brace
                after = code[end_brace+1:]
                alias_m = re.match(r'\s*(\w+)\s*;', after)
                if alias_m:
                    typedef_name = alias_m.group(1)
                    nested_reg = dict(self.type_registry)
                    fields = parse_struct_body(body, nested_reg)
                    self.type_registry.update(nested_reg)
                    sd = StructDef(typedef_name, fields, is_class=(kw == 'class'))
                    self.type_registry[typedef_name] = sd
                    if tag_name:
                        self.type_registry[tag_name] = sd
                    pos = end_brace + 1 + alias_m.end()
                else:
                    pos += m2.end()
                continue

            # Try typedef simple alias: typedef existing_type new_name;
            m3 = re.match(
                r'\btypedef\s+((?:(?:unsigned|signed|long|short|const|volatile|struct|union|enum|class)\s+)*\w[\w:]*(?:\s*\*)*)\s+(\w+)\s*;',
                code[pos:]
            )
            if m3:
                src_type = m3.group(1).strip()
                dst_name = m3.group(2)
                if dst_name not in self.type_registry:
                    self.type_registry[dst_name] = src_type
                pos += m3.end()
                continue

            pos += 1


# ---------------------------------------------------------------------------
# Layout calculator
# ---------------------------------------------------------------------------

class LayoutField:
    """Represents a field with computed offset and size."""
    def __init__(self, name: str, type_name: str, offset: int, size: int,
                 align: int, is_pointer: bool, pointer_depth: int,
                 array_dims: list, children: list = None):
        self.name = name
        self.type_name = type_name
        self.offset = offset       # byte offset from struct start
        self.size = size           # total size in bytes (including array)
        self.align = align         # alignment requirement
        self.is_pointer = is_pointer
        self.pointer_depth = pointer_depth
        self.array_dims = array_dims   # e.g. [2] for arr[2]
        self.children = children or []  # list of LayoutField for struct members


class LayoutCalculator:
    def __init__(self, type_registry: dict, bits: int = 64):
        self.type_registry = type_registry
        self.bits = bits
        self.pointer_size = 8 if bits == 64 else 4
        self._cache = {}   # type_name -> (size, align)

    def get_type_info(self, field: Field) -> tuple:
        """
        Returns (size_per_element, align, children_layout).
        size_per_element: size of one element (not counting array multiplier).
        children_layout: list of LayoutField if this is a struct, else [].
        """
        if field.is_pointer:
            return self.pointer_size, self.pointer_size, []

        base = field.base_type
        # Resolve alias chain
        base = self._resolve_alias(base)

        # Check basic type
        result = get_basic_size_align(base, self.bits)
        if result is not None:
            return result[0], result[1], []

        # Check if it's a struct
        td = self.type_registry.get(base)
        if isinstance(td, StructDef):
            size, align, children = self._layout_struct(td)
            return size, align, children

        # Unknown type - assume 4 bytes
        return 4, 4, []

    def _resolve_alias(self, name: str) -> str:
        """Resolve typedef chains."""
        visited = set()
        current = name
        while current in self.type_registry:
            val = self.type_registry[current]
            if isinstance(val, str) and val != current:
                if val in visited:
                    break
                visited.add(val)
                current = val
            else:
                break
        return current

    def _layout_struct(self, sd: StructDef) -> tuple:
        """
        Compute the layout of a struct/class.
        Returns (total_size, alignment, list_of_LayoutField).
        """
        if sd.name in self._cache:
            size, align = self._cache[sd.name]
            # Recompute children (not cached)
            # For simplicity, just return cached size/align and recompute layout

        offset = 0
        max_align = 1
        layout_fields = []

        for field in sd.fields:
            elem_size, elem_align, children = self.get_type_info(field)

            # For arrays, the element size is what we got; total = elem_size * product(dims)
            if field.array_dims:
                count = 1
                for d in field.array_dims:
                    count *= d
                total_field_size = elem_size * count
            else:
                count = 1
                total_field_size = elem_size

            # Natural alignment: align field to its own alignment
            offset = align_up(offset, elem_align)
            max_align = max(max_align, elem_align)

            lf = LayoutField(
                name=field.name,
                type_name=field.base_type,
                offset=offset,
                size=total_field_size,
                align=elem_align,
                is_pointer=field.is_pointer,
                pointer_depth=field.pointer_depth,
                array_dims=field.array_dims,
                children=children,
            )
            layout_fields.append(lf)
            offset += total_field_size

        # Pad struct to multiple of its own alignment
        total_size = align_up(offset, max_align)

        self._cache[sd.name] = (total_size, max_align)
        return total_size, max_align, layout_fields

    def layout_type(self, type_name: str) -> tuple:
        """
        Public entry: compute layout for a named type.
        Returns (total_size, align, list_of_LayoutField).
        """
        resolved = self._resolve_alias(type_name)
        td = self.type_registry.get(resolved)
        if isinstance(td, StructDef):
            return self._layout_struct(td)
        # Basic type
        result = get_basic_size_align(resolved, self.bits)
        if result:
            return result[0], result[1], []
        return 4, 4, []


# ---------------------------------------------------------------------------
# GDB output parser
# ---------------------------------------------------------------------------

def parse_gdb_output(text: str) -> tuple:
    """
    Parse GDB x/nx output.
    Returns (base_address, word_size, words) where:
      - base_address: int
      - word_size: bytes per word (4 for x/nx, 8 for x/ngx, etc.)
      - words: list of int (in order)

    Handles both formats:
      0xf0000000: 0x00000001 0x00000002 ...
      0xf0000000:	0x00000001 0x00000002 ...
    """
    words = []
    base_address = None
    word_size = 4   # default: x/nwx uses 4-byte words

    for line in text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        # Match address: value value ...
        m = re.match(r'(0x[0-9a-fA-F]+)\s*:\s*(.*)', line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        if base_address is None:
            base_address = addr
        vals_str = m.group(2)
        for val_m in re.finditer(r'0x([0-9a-fA-F]+)', vals_str):
            words.append(int(val_m.group(1), 16))

    return base_address, word_size, words


def infer_bits_from_address(base_address: int) -> int:
    """Infer 32 or 64 bit from address width.
    Addresses above 4GB are definitely 64-bit.
    Addresses in 32-bit range are ambiguous; default to 64-bit (modern systems).
    Use --bits to override explicitly.
    """
    if base_address is None:
        return 64
    return 64 if base_address > 0xFFFFFFFF else 64  # default 64-bit


# ---------------------------------------------------------------------------
# Memory reader
# ---------------------------------------------------------------------------

class MemoryReader:
    """Reads typed values from a byte array."""

    def __init__(self, data: bytes, base_address: int, bits: int):
        self.data = data
        self.base_address = base_address
        self.bits = bits

    def read_bytes(self, offset: int, size: int) -> bytes:
        if offset + size > len(self.data):
            # Return zeros for out-of-bounds
            available = max(0, len(self.data) - offset)
            return self.data[offset:offset+available] + b'\x00' * (size - available)
        return self.data[offset:offset+size]

    def read_int(self, offset: int, size: int) -> int:
        """Read little-endian integer."""
        b = self.read_bytes(offset, size)
        result = 0
        for i, byte in enumerate(b):
            result |= byte << (8 * i)
        return result

    def format_value(self, offset: int, size: int) -> str:
        """Format a value at offset as hex string."""
        val = self.read_int(offset, size)
        hex_digits = size * 2
        return f'0x{val:0{hex_digits}x}'


# ---------------------------------------------------------------------------
# Output formatter
# ---------------------------------------------------------------------------

MAX_NESTING = 64


def format_layout(layout_fields: list, reader: MemoryReader,
                  struct_name: str, var_name: str,
                  indent: int = 0, depth: int = 0,
                  base_offset: int = 0) -> list:
    """
    Recursively format the struct layout using memory data.
    Returns list of strings (lines).
    """
    lines = []
    pad = '  ' * indent

    if depth == 0:
        lines.append(f'{pad}{struct_name} {var_name}:')

    for lf in layout_fields:
        field_offset = base_offset + lf.offset
        field_pad = '  ' * (indent + 1)

        if lf.is_pointer:
            # Pointer: read pointer_size bytes
            ptr_size = 8 if reader.bits == 64 else 4
            val = reader.format_value(field_offset, ptr_size)
            lines.append(f'{field_pad}.{lf.name} = {val}')

        elif lf.array_dims and not lf.children:
            # Array of basic type
            elem_size = lf.size // (lf.array_dims[0] if lf.array_dims else 1)
            # Multi-dim: flatten for now
            count = lf.size // (lf.align if lf.align > 0 else 1)
            # Recalculate correctly
            total_count = 1
            for d in lf.array_dims:
                total_count *= d
            elem_size = lf.size // total_count if total_count > 0 else lf.size
            for i in range(total_count):
                idx_str = f'[{i}]'
                val = reader.format_value(field_offset + i * elem_size, elem_size)
                lines.append(f'{field_pad}.{lf.name}{idx_str} = {val}')

        elif lf.array_dims and lf.children:
            # Array of struct
            if depth >= MAX_NESTING:
                lines.append(f'{field_pad}.{lf.name}[...] (max nesting reached)')
                continue
            total_count = 1
            for d in lf.array_dims:
                total_count *= d
            elem_size = lf.size // total_count if total_count > 0 else lf.size
            for i in range(total_count):
                lines.append(f'{field_pad}{lf.type_name} {lf.name}[{i}]:')
                sub_lines = format_layout(
                    lf.children, reader,
                    lf.type_name, lf.name,
                    indent=indent + 1, depth=depth + 1,
                    base_offset=field_offset + i * elem_size
                )
                lines.extend(sub_lines)

        elif lf.children:
            # Nested struct
            if depth >= MAX_NESTING:
                lines.append(f'{field_pad}{lf.type_name} {lf.name} (max nesting reached)')
                continue
            sub_lines = format_layout(
                lf.children, reader,
                lf.type_name, lf.name,
                indent=indent + 1, depth=depth + 1,
                base_offset=field_offset
            )
            # The first sub_line is "TypeName name:" header - insert with current indent
            if sub_lines:
                lines.append(f'{field_pad}{lf.type_name} {lf.name}:')
                lines.extend(sub_lines)

        else:
            # Basic scalar
            val = reader.format_value(field_offset, lf.size)
            # char: show as single byte
            if lf.type_name in ('char', 'signed char', 'unsigned char'):
                val = reader.format_value(field_offset, 1)
            lines.append(f'{field_pad}.{lf.name} = {val}')

    return lines


# ---------------------------------------------------------------------------
# Build system source file finder
# ---------------------------------------------------------------------------

def find_sources_cmake(build_dir: str) -> list:
    """Try to find source files from compile_commands.json."""
    cc_json = os.path.join(build_dir, 'compile_commands.json')
    if not os.path.exists(cc_json):
        # Try to find it recursively
        for root, dirs, files in os.walk(build_dir):
            if 'compile_commands.json' in files:
                cc_json = os.path.join(root, 'compile_commands.json')
                break
        else:
            return []

    try:
        with open(cc_json, 'r') as f:
            entries = json.load(f)
        files = []
        for entry in entries:
            fpath = entry.get('file', '')
            if fpath and os.path.exists(fpath):
                files.append(fpath)
        return files
    except Exception:
        return []


def find_sources_makefile(build_dir: str) -> list:
    """Try 'make -n' to find source files."""
    makefile = os.path.join(build_dir, 'Makefile')
    if not os.path.exists(makefile):
        return []
    try:
        result = subprocess.run(
            ['make', '-n', '-C', build_dir],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
        files = []
        for m in re.finditer(r'[\w./\-]+\.(?:c|cpp|cc|cxx|C)', output):
            fpath = m.group(0)
            if not os.path.isabs(fpath):
                fpath = os.path.join(build_dir, fpath)
            fpath = os.path.normpath(fpath)
            if os.path.exists(fpath):
                files.append(fpath)
        return list(set(files))
    except Exception:
        return []


def find_sources_bazel(build_dir: str) -> list:
    """Scan for BUILD files and extract source lists."""
    files = []
    for root, dirs, fnames in os.walk(build_dir):
        for fname in fnames:
            if fname in ('BUILD', 'BUILD.bazel'):
                build_file = os.path.join(root, fname)
                try:
                    with open(build_file, 'r') as f:
                        content = f.read()
                    for m in re.finditer(r'"([\w./\-]+\.(?:c|cpp|cc|cxx|h|hpp))"', content):
                        rel = m.group(1)
                        fpath = os.path.join(root, rel)
                        fpath = os.path.normpath(fpath)
                        if os.path.exists(fpath):
                            files.append(fpath)
                except Exception:
                    pass
    return files


def find_sources_fallback(search_dirs: list) -> list:
    """Recursively find all C/C++ source and header files."""
    files = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, dirs, fnames in os.walk(d):
            # Skip hidden directories and common non-source dirs
            dirs[:] = [x for x in dirs if not x.startswith('.') and x not in
                       ('build', 'cmake-build', '.git', 'node_modules', '__pycache__')]
            for fname in fnames:
                if fname.endswith(('.h', '.hpp', '.hxx', '.c', '.cpp', '.cc', '.cxx', '.C')):
                    files.append(os.path.join(root, fname))
    return files


def find_source_files(build_dir: str, src_dirs: list) -> list:
    """
    Find all relevant source files using available build system info.
    Returns a deduplicated list of file paths.
    """
    files = []

    if build_dir:
        # Try in order: CMake, Makefile, Bazel
        cmake_files = find_sources_cmake(build_dir)
        if cmake_files:
            print(f"[info] Found {len(cmake_files)} files via compile_commands.json")
            files.extend(cmake_files)

        if not files:
            mk_files = find_sources_makefile(build_dir)
            if mk_files:
                print(f"[info] Found {len(mk_files)} files via Makefile")
                files.extend(mk_files)

        if not files:
            bz_files = find_sources_bazel(build_dir)
            if bz_files:
                print(f"[info] Found {len(bz_files)} files via Bazel BUILD files")
                files.extend(bz_files)

    if not files:
        search = src_dirs if src_dirs else ([build_dir] if build_dir else ['.'])
        files = find_sources_fallback(search)
        print(f"[info] Fallback: found {len(files)} files by directory scan")

    # Deduplicate, prioritize header files first
    seen = set()
    result = []
    for f in files:
        f = os.path.abspath(f)
        if f not in seen:
            seen.add(f)
            result.append(f)

    # Sort: headers first, then sources
    result.sort(key=lambda x: (0 if x.endswith(('.h', '.hpp', '.hxx')) else 1, x))
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def words_to_bytes(words: list, word_size: int = 4) -> bytes:
    """Convert list of integer words to a little-endian byte array."""
    result = bytearray()
    for w in words:
        for i in range(word_size):
            result.append((w >> (8 * i)) & 0xFF)
    return bytes(result)


def run(args):
    # 1. Find and parse source files
    src_dirs = [s.strip() for s in args.src_dirs.split(',')] if args.src_dirs else []
    source_files = find_source_files(args.build_dir, src_dirs)

    if not source_files:
        print("[warning] No source files found. Type definitions may be missing.")

    parser = TypeParser()
    for fpath in source_files:
        parser.parse_file(fpath)

    print(f"[info] Parsed {len(source_files)} files, "
          f"{sum(1 for v in parser.type_registry.values() if isinstance(v, StructDef))} struct/class types found")

    # 2. Parse GDB output
    with open(args.gdb, 'r') as f:
        gdb_text = f.read()

    base_address, word_size, words = parse_gdb_output(gdb_text)
    if not words:
        print("[error] No data parsed from GDB output.")
        sys.exit(1)

    # 3. Determine bits
    if args.bits:
        bits = args.bits
    else:
        bits = infer_bits_from_address(base_address)
    print(f"[info] Using {bits}-bit mode, base address: "
          f"{hex(base_address) if base_address is not None else 'unknown'}, "
          f"{len(words)} words ({len(words)*word_size} bytes)")

    # 4. Compute layout
    calc = LayoutCalculator(parser.type_registry, bits=bits)
    type_name = args.type

    # Resolve type
    resolved = calc._resolve_alias(type_name)
    td = parser.type_registry.get(resolved)
    if td is None:
        print(f"[error] Type '{type_name}' not found in parsed sources.")
        print(f"[info] Available types: {[k for k,v in parser.type_registry.items() if isinstance(v, StructDef)][:20]}")
        sys.exit(1)

    total_size, align, layout_fields = calc.layout_type(type_name)
    data_bytes = words_to_bytes(words, word_size)

    avail = len(data_bytes)
    print(f"[info] sizeof({type_name}) = {total_size} bytes, available data = {avail} bytes")
    if avail < total_size:
        print(f"[warning] Available data ({avail} bytes) < sizeof({type_name}) ({total_size} bytes). "
              f"Fields beyond byte {avail} will show as 0x00...")

    # 5. Format output
    reader = MemoryReader(data_bytes, base_address, bits)
    var_name = args.var_name if args.var_name else 'var'
    lines = format_layout(layout_fields, reader, type_name, var_name)
    output_text = '\n'.join(lines) + '\n'

    # 6. Write output
    if args.output:
        out_path = args.output
    else:
        base = os.path.splitext(args.gdb)[0]
        out_path = base + '_mapped.txt'

    with open(out_path, 'w') as f:
        f.write(output_text)

    print(f"[info] Output written to: {out_path}")
    print()
    print(output_text)


def main():
    p = argparse.ArgumentParser(
        description='Map GDB x/nx memory dump to C/C++ struct fields'
    )
    p.add_argument('--type', required=True,
                   help='C/C++ type name to map (e.g. "var", "StructC")')
    p.add_argument('--gdb', required=True,
                   help='Path to file containing GDB x/nx output')
    p.add_argument('--build-dir', default=None,
                   help='Build directory (for compile_commands.json / Makefile / Bazel)')
    p.add_argument('--src-dirs', default=None,
                   help='Comma-separated list of source directories to scan')
    p.add_argument('--bits', type=int, choices=[32, 64], default=None,
                   help='Pointer size: 32 or 64 (auto-detected from address if omitted)')
    p.add_argument('--var-name', default=None,
                   help='Variable name for output display (default: "var")')
    p.add_argument('--output', default=None,
                   help='Output file path (default: <gdb-file>_mapped.txt)')

    args = p.parse_args()
    run(args)


if __name__ == '__main__':
    main()
