#!/usr/bin/env python3
"""Source-order guard for the macOS daemonize fix (upstream issues 373, 388).

macFUSE 5.3.3+ mounts lazily: fuse_mount() only records the request and the
real mount starts on first use of the session. fuse_session_fd() is such a
first use, and once the mount has started fuse_daemonize() refuses to fork.
So on macOS main() must not touch the session between fuse_mount() and
fuse_daemonize(). The Linux tests cannot see this (they all run with -f), so
check the source. No FUSE needed.

What this checks: it lexes sshfs.c, evaluates the preprocessor conditions in
main() for a macOS build, and fails if a name from MOUNT_TRIGGERS appears in
main() after fuse_mount() and before fuse_daemonize(), in source order. A
condition it cannot evaluate fails the test instead of being guessed.

What it does not check: execution order (a conditional fuse_daemonize(), a
goto), calls made through other functions, macros that hide a call, and
function pointers set up outside that stretch of main(). It is a tripwire for
the shape of the 3.7.6 bug, not proof; running sshfs on macOS is the real test.
"""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import re
import pytest

# Names that start the delayed macFUSE mount when used on the session. Any
# mention counts, so taking the address of one is caught too.
MOUNT_TRIGGERS = frozenset(
    (
        "fuse_session_fd",
        "fuse_session_receive_buf",
        "fuse_session_loop",
        "fuse_session_loop_mt",
        "fuse_loop",
        "fuse_loop_mt",
    )
)

# Calls that arm the delayed mount
MOUNT_CALLS = frozenset(("fuse_mount", "fuse_session_mount"))

# What a macOS build defines: __APPLE__ from the compiler, the rest from
# add_global_arguments() and c_args in meson.build
MACOS_DEFINED = {
    "__APPLE__": 1,
    "__MACH__": 1,
    "_REENTRANT": 1,
    "HAVE_CONFIG_H": 1,
    "FUSE_USE_VERSION": 31,
    "FUSE_DARWIN_ENABLE_EXTENSIONS": 0,
}
# Platform macros known to be absent there. Any other macro in a condition is
# unknown, which is an error.
MACOS_UNDEFINED = frozenset(
    (
        "__CYGWIN__",
        "__linux__",
        "__FreeBSD__",
        "__NetBSD__",
        "__OpenBSD__",
        "__GLIBC__",
        "_WIN32",
    )
)


class Unsupported(Exception):
    """The source has something this test cannot read safely"""


_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t\r\f\v]+)
  | (?P<nl>\n)
  | (?P<lcomment>//(?:\\.|[^\n\\])*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*")
  | (?P<chr>'(?:\\.|[^'\\\n])*')
  | (?P<num>(?:0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*)
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<punct>&&|\|\||==|!=|<=|>=|<<|>>|->|\+\+|--|[-+*/%&|^~!<>=?:;,.(){}\[\]\#])
  | (?P<other>.)
    """,
    re.X | re.S,
)


def lex(text):
    """Split C source into tokens

    A token is (kind, value, line). Comments and whitespace are dropped, string
    and character literals are single tokens, and a preprocessor directive is
    one token ("directive", name, args, line) where args are the tokens after
    the name. Lines ending in a backslash are joined first, as in C.
    """
    cuts = []  # where in the joined text a backslash-newline was removed
    pieces = text.split("\\\n")
    text = ""
    for piece in pieces[:-1]:
        text += piece
        cuts.append(len(text))
    text += pieces[-1]

    tokens = []
    line = 1
    cut = 0
    at_line_start = True
    directive = None  # [line, tokens] while inside one

    def end_directive():
        first, args = directive
        name = args[0][1] if args and args[0][0] == "id" else ""
        tokens.append(("directive", name, args[1:] if name else args, first))

    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        kind, val = m.lastgroup, m.group()
        while cut < len(cuts) and cuts[cut] <= pos:
            line += 1
            cut += 1
        pos = m.end()
        start = line
        line += val.count("\n")
        if kind == "nl":
            if directive is not None:
                end_directive()
                directive = None
            at_line_start = True
        elif kind in ("ws", "lcomment", "bcomment"):
            pass
        elif kind == "punct" and val == "#" and at_line_start and directive is None:
            directive = [start, []]
            at_line_start = False
        else:
            at_line_start = False
            (directive[1] if directive is not None else tokens).append(
                (kind, val, start)
            )
    if directive is not None:
        end_directive()
    return tokens


_BINARY = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    ">": 7,
    "<=": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}


def _integer(text):
    """Value of an integer literal; unsigned ones are not supported, since the
    preprocessor's unsigned arithmetic differs from Python's"""
    digits = re.sub(r"[uUlL]+$", "", text)
    if "u" in text[len(digits) :].lower():
        raise Unsupported(f"unsigned integer {text!r}")
    try:
        if digits[:2] in ("0x", "0X"):
            value = int(digits, 16)
        elif digits != "0" and digits.startswith("0"):
            value = int(digits, 8)
        else:
            value = int(digits)
    except ValueError:
        raise Unsupported(f"bad integer {text!r}") from None
    if value >= 2**63:
        raise Unsupported(f"unsigned integer {text!r}")
    return value


def _apply(op, a, b):
    if op in ("/", "%"):
        if b == 0:
            raise Unsupported("division by zero in a preprocessor condition")
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        return q if op == "/" else a - q * b
    return int(
        {
            "||": lambda: bool(a) or bool(b),
            "&&": lambda: bool(a) and bool(b),
            "|": lambda: a | b,
            "^": lambda: a ^ b,
            "&": lambda: a & b,
            "==": lambda: a == b,
            "!=": lambda: a != b,
            "<": lambda: a < b,
            ">": lambda: a > b,
            "<=": lambda: a <= b,
            ">=": lambda: a >= b,
            "<<": lambda: a << b,
            ">>": lambda: a >> b,
            "+": lambda: a + b,
            "-": lambda: a - b,
            "*": lambda: a * b,
        }[op]()
    )


def eval_cond(args, macros=MACOS_DEFINED, absent=MACOS_UNDEFINED):
    """Evaluate the tokens of an #if/#elif condition

    Supports integers, defined(), the C operators on integers, and the macros
    in *macros* (with their values) and *absent* (undefined, so 0). Anything
    else raises Unsupported.
    """
    toks = [(kind, val) for (kind, val, _line) in args]
    pos = 0

    def peek():
        return toks[pos][1] if pos < len(toks) else None

    def take():
        nonlocal pos
        if pos >= len(toks):
            raise Unsupported("condition ends too early")
        pos += 1
        return toks[pos - 1]

    def expect(val):
        if take()[1] != val:
            raise Unsupported(f"expected {val!r} in condition")

    def is_defined(name):
        if name in macros:
            return True
        if name in absent:
            return False
        raise Unsupported(f"don't know whether {name} is defined on macOS")

    def primary():
        kind, val = take()
        if val == "(":
            result = ternary()
            expect(")")
            return result
        if val in ("!", "~", "-", "+"):
            operand = primary()
            return {"!": int(not operand), "~": ~operand, "-": -operand, "+": operand}[
                val
            ]
        if kind == "num":
            return _integer(val)
        if kind == "id" and val == "defined":
            if peek() == "(":
                take()
                name = take()[1]
                expect(")")
            else:
                name = take()[1]
            return int(is_defined(name))
        if kind == "id":
            if val in macros:
                if macros[val] is None:
                    raise Unsupported(f"don't know the value of {val}")
                return macros[val]
            if val in absent:
                return 0
            raise Unsupported(f"don't know the value of {val} on macOS")
        raise Unsupported(f"can't evaluate {val!r}")

    def binary(min_prec):
        left = primary()
        while peek() in _BINARY and _BINARY[peek()] >= min_prec:
            op = take()[1]
            left = _apply(op, left, binary(_BINARY[op] + 1))
        return left

    def ternary():
        cond = binary(1)
        if peek() == "?":
            take()
            yes = ternary()
            expect(":")
            no = ternary()
            return yes if cond else no
        return cond

    result = ternary()
    if pos != len(toks):
        raise Unsupported(f"can't evaluate {toks[pos][1]!r}")
    return result != 0


def _track_macro(name, args, macros, absent):
    """Apply #define or #undef; a macro without a plain integer value gets None"""
    if not args or args[0][0] != "id":
        return
    macro = args[0][1]
    if name == "undef":
        macros.pop(macro, None)
        absent.add(macro)
        return
    absent.discard(macro)
    value = None
    if len(args) == 2 and args[1][0] == "num":
        try:
            value = _integer(args[1][1])
        except Unsupported:
            pass
    macros[macro] = value


def _directive_cond(name, args, macros, absent):
    if name in ("ifdef", "ifndef"):
        if len(args) != 1 or args[0][0] != "id":
            raise Unsupported(f"can't read #{name}")
        defined = eval_cond([("id", "defined", 0), args[0]], macros, absent)
        return defined if name == "ifdef" else not defined
    return eval_cond(args, macros, absent)


def main_body(tokens, macros=MACOS_DEFINED, absent=MACOS_UNDEFINED):
    """Return the tokens inside main() that are compiled with *macros*

    Conditions are evaluated leniently up to the start of main() and strictly
    from there to its closing brace; one that can't be evaluated is an error.
    """
    macros, absent = dict(macros), set(absent)
    start = next(
        (
            i
            for i, t in enumerate(tokens[:-2])
            if t[1:2] == ("int",)
            and tokens[i + 1][1] == "main"
            and tokens[i + 2][1] == "("
        ),
        None,
    )
    if start is None:
        raise Unsupported("can't find 'int main(' in the source")

    stack = []  # [parent_active, branch_taken, evaluated]
    active = True
    state = "before"
    paren = brace = 0
    body = []
    for index, tok in enumerate(tokens):
        if index == start:
            if any(not frame[2] for frame in stack):
                raise Unsupported(
                    "main() is inside a condition that can't be evaluated"
                )
            state = "find"
        if tok[0] == "directive":
            _kind, name, args, line = tok
            strict = index >= start
            if name in ("if", "ifdef", "ifndef", "elif"):
                ok = True
                cond = False
                if name == "elif":
                    if not stack:
                        raise Unsupported(f"line {line}: #elif without #if")
                    frame = stack.pop()
                    parent, taken, ok = frame
                    evaluate = parent and not taken
                else:
                    parent, taken = active, False
                    evaluate = active
                if evaluate:
                    try:
                        cond = _directive_cond(name, args, macros, absent)
                    except Unsupported as exc:
                        if strict:
                            raise Unsupported(f"line {line}: #{name}: {exc}") from None
                        ok = False
                stack.append([parent, taken or cond, ok])
                active = parent and cond
            elif name == "else":
                if not stack:
                    raise Unsupported(f"line {line}: #else without #if")
                frame = stack[-1]
                active = frame[0] and not frame[1]
                frame[1] = True
            elif name == "endif":
                if not stack:
                    raise Unsupported(f"line {line}: #endif without #if")
                active = stack.pop()[0]
            elif name in ("define", "undef"):
                if active:
                    _track_macro(name, args, macros, absent)
            elif name not in ("include", "pragma", "error", "warning", "line", ""):
                raise Unsupported(f"line {line}: unknown directive #{name}")
            continue
        if not active or state == "before":
            continue
        val = tok[1]
        if state == "find":
            if tok[0] == "id" and val == "main":
                state = "params"
        elif state == "params":
            if val == "(":
                paren += 1
            elif val == ")":
                paren -= 1
                if paren == 0:
                    state = "brace"
        elif state == "brace":
            if val != "{":
                raise Unsupported(f"line {tok[2]}: expected '{{' after main()")
            state, brace = "body", 1
        else:
            if val == "{":
                brace += 1
            elif val == "}":
                brace -= 1
                if brace == 0:
                    return body
            body.append(tok)
    raise Unsupported("can't find the end of main()")


def early_mount_triggers(text, macros=MACOS_DEFINED, absent=MACOS_UNDEFINED):
    """Return [(line, name)] for mount triggers between fuse_mount() and
    fuse_daemonize() in main()"""
    body = main_body(lex(text), macros, absent)

    def mentions(names):
        return [i for i, t in enumerate(body) if t[0] == "id" and t[1] in names]

    mounts = mentions(MOUNT_CALLS)
    daemonize = mentions(("fuse_daemonize",))
    if not mounts:
        raise Unsupported("no fuse_mount() call in main()")
    if len(daemonize) != 1 or not (
        daemonize[0] + 1 < len(body) and body[daemonize[0] + 1][1] == "("
    ):
        raise Unsupported("expected exactly one plain fuse_daemonize() call in main()")
    return [
        (t[2], t[1])
        for t in body[mounts[0] + 1 : daemonize[0]]
        if t[0] == "id" and t[1] in MOUNT_TRIGGERS
    ]


def find_sshfs_c():
    """Return the path of sshfs.c

    When running from the source tree it is one directory up. test/meson.build
    copies it next to the tests for build directories, so rebuild with ninja
    after editing it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "..", "sshfs.c"), os.path.join(here, "sshfs.c")):
        if os.path.exists(path):
            return os.path.normpath(path)
    pytest.fail("sshfs.c not found next to the tests or in the parent directory")


def names(text, **kwargs):
    return [name for _line, name in early_mount_triggers(text, **kwargs)]


def cond(expr):
    return eval_cond(lex("#if " + expr + "\n")[0][2])


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("__APPLE__", True),
        ("defined(__APPLE__)", True),
        ("defined __APPLE__", True),
        ("!defined(__APPLE__)", False),
        ("!defined(__CYGWIN__) && !defined(__APPLE__)", False),
        ("defined(__CYGWIN__) || defined(__APPLE__)", True),
        ("defined(__CYGWIN__) && defined(__APPLE__)", False),
        ("!(defined(__CYGWIN__) || defined(__APPLE__))", False),
        # && binds tighter than ||
        ("defined(__APPLE__) || defined(__linux__) && defined(__CYGWIN__)", True),
        ("defined(__linux__) && defined(__CYGWIN__) || defined(__APPLE__)", True),
        ("defined(__linux__) || defined(__CYGWIN__) && defined(__APPLE__)", False),
        ("1", True),
        ("0", False),
        ("0x1", True),
        ("0X0", False),
        ("1L", True),
        ("1L", True),
        ("1LL", True),
        ("010 == 8", True),
        ("FUSE_USE_VERSION >= 30", True),
        ("FUSE_USE_VERSION < 31", False),
        ("FUSE_USE_VERSION == 31", True),
        ("FUSE_DARWIN_ENABLE_EXTENSIONS", False),
        ("__linux__", False),
        ("1 ? 0 : 1", False),
        ("(2 + 3) * 4 == 20", True),
        ("-7 / 2 == -3", True),
        ("1 << 4 == 16", True),
    ],
)
def test_eval_cond(expr, expected):
    assert cond(expr) is expected


@pytest.mark.parametrize(
    "expr",
    [
        "SOME_UNKNOWN_MACRO",
        "defined(SOME_UNKNOWN_MACRO)",
        "FUSE_VERSION >= 32",
        "__has_include(<fuse.h>)",
        "1 +",
        "(1",
        "defined",
        "1 / 0",
        "'a' == 97",
        "09",
        "1U",
        "~0U > 0",
        "0xFFFFFFFFFFFFFFFF",
    ],
)
def test_eval_cond_unsupported(expr):
    with pytest.raises(Unsupported):
        cond(expr)


def main_with(between, after=""):
    return (
        "int main(int argc, char *argv[])\n{\n"
        "\tres = fuse_mount(fuse, mountpoint);\n"
        + between
        + "\n\tres = fuse_daemonize(0);\n"
        + after
        + "\n\tres = fuse_loop(fuse);\n}\n"
    )


# main() as it was in sshfs 3.7.6, which broke daemonizing on macOS
BAD_MAIN = """
int main(int argc, char *argv[])
{
	res = fuse_mount(fuse, sshfs.mountpoint);
#if !defined(__CYGWIN__)
	res = fcntl(fuse_session_fd(se), F_SETFD, FD_CLOEXEC);
#endif
	res = fuse_daemonize(sshfs.foreground);
	res = fuse_loop(fuse);
}
"""

# the fix: the fcntl() moves behind fuse_daemonize() on macOS
GOOD_MAIN = """
#ifdef __APPLE__
int main(int argc, char *argv[], char *envp[], char **exec_path)
#else
int main(int argc, char *argv[])
#endif
{
	res = fuse_mount(fuse, sshfs.mountpoint);
	/* fuse_session_fd() would start the mount here */
#if !defined(__CYGWIN__) && !defined(__APPLE__)
	res = fcntl(fuse_session_fd(se), F_SETFD, FD_CLOEXEC);
#endif
	res = fuse_daemonize(sshfs.foreground);
#ifdef __APPLE__
	res = fcntl(fuse_session_fd(se), F_SETFD, FD_CLOEXEC);
#endif
	res = fuse_loop(fuse);
}
"""


def test_3_7_6_shape_is_flagged():
    assert early_mount_triggers(BAD_MAIN) == [(6, "fuse_session_fd")]


def test_fixed_shape_is_accepted():
    assert early_mount_triggers(GOOD_MAIN) == []
    # without the __APPLE__ guard on the first fcntl() it would be flagged
    unguarded = GOOD_MAIN.replace(" && !defined(__APPLE__)", "")
    assert names(unguarded) == ["fuse_session_fd"]
    # the other main() signature parses too; without __APPLE__ the guard
    # doesn't exclude the first fcntl()
    other = {"macros": {"FUSE_USE_VERSION": 31}, "absent": {"__APPLE__", "__CYGWIN__"}}
    assert names(GOOD_MAIN, **other) == ["fuse_session_fd"]


def else_branch(head):
    """An #if whose #else branch calls fuse_session_fd()"""
    return head + "\n\t(void)0;\n#else\n\t(void)fuse_session_fd(se);\n#endif"


# Each of these is a regression a naive scanner can miss
FLAGGED = {
    "plain call": main_with("\tfcntl(fuse_session_fd(se), F_SETFD, 1);"),
    "call split over lines": main_with("\t(void)fuse_session_fd\n\t\t(se);"),
    "all on one line": (
        "int main(void) { res = fuse_mount(f, m); (void)fuse_session_fd(se); "
        "res = fuse_daemonize(0); }"
    ),
    "address taken": main_with(
        "\tint (*getfd)(struct fuse_session *) = fuse_session_fd;"
    ),
    "receive_buf": main_with("\tfuse_session_receive_buf(se, &buf);"),
    "session_loop": main_with("\tfuse_session_loop(se);"),
    "session_loop_mt": main_with("\tfuse_session_loop_mt(se, NULL);"),
    "fuse_loop": main_with("\tfuse_loop(fuse);"),
    "fuse_loop_mt": main_with("\tfuse_loop_mt(fuse, 0);"),
    "open brace on the signature line": (
        "int main(int argc, char **argv) {\n\tfuse_mount(f, m);\n"
        "\t(void)fuse_session_fd(se);\n\tif (c)\n\t{\n\t\tx();\n\t}\n"
        "\tres = fuse_daemonize(0);\n}\n"
    ),
    "space before the argument list": (
        "int main (int argc, char **argv)\n{\n\tfuse_mount(f, m);\n"
        "\t(void)fuse_session_fd(se);\n\tfuse_daemonize(0);\n}\n"
    ),
    "after a block comment opener in a string": main_with(
        '\tprintf("/*");\n\t(void)fuse_session_fd(se);\n\tprintf("*/");'
    ),
    "after // in a string": main_with(
        '\tprintf("https://example.org"); (void)fuse_session_fd(se);'
    ),
    "after a quote in a char literal": main_with(
        "\tchar q = '\"'; (void)fuse_session_fd(se);"
    ),
    "else after a version comparison": main_with(
        else_branch("#if FUSE_USE_VERSION < 31")
    ),
    "elif after a false branch": main_with(
        "#if 0\n#elif defined(__APPLE__)\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "ifdef of a macro the build defines": main_with(
        "#ifdef FUSE_USE_VERSION\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "hex condition": main_with("#if 0x1\n\t(void)fuse_session_fd(se);\n#endif"),
    "suffixed condition": main_with("#if 1L\n\t(void)fuse_session_fd(se);\n#endif"),
    "identifier split by a line splice": main_with("\t(void)fuse_session_\\\nfd(se);"),
    "macro undefined in main": main_with(
        "#undef FUSE_DARWIN_ENABLE_EXTENSIONS\n#ifndef FUSE_DARWIN_ENABLE_EXTENSIONS\n"
        "\t(void)fuse_session_fd(se);\n#endif"
    ),
    "macro defined in main": main_with(
        "#define MY_FLAG 1\n#if MY_FLAG\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "octal condition": main_with("#if 010 == 8\n\t(void)fuse_session_fd(se);\n#endif"),
    "ifndef of an absent macro": main_with(
        "#ifndef __CYGWIN__\n\t(void)fuse_session_fd(se);\n#endif"
    ),
}

# These are fine and must not be flagged
ACCEPTED = {
    "after fuse_daemonize": main_with("", after="\t(void)fuse_session_fd(se);"),
    "daemonize first on a line": (
        "int main(void) { res = fuse_mount(f, m); res = fuse_daemonize(0); "
        "(void)fuse_session_fd(se); }"
    ),
    "other session calls": main_with(
        "\tse = fuse_get_session(fuse);\n\tfuse_set_signal_handlers(se);\n"
        "\tfuse_session_exit(se);"
    ),
    "before fuse_mount": (
        "int main(void) { (void)fuse_session_fd(se); res = fuse_mount(f, m); "
        "res = fuse_daemonize(0); }"
    ),
    "in a string": main_with('\tprintf("fuse_session_fd() was called");'),
    "in a block comment": main_with("\t/* (void)fuse_session_fd(se); */"),
    "in a line comment": main_with("\t// (void)fuse_session_fd(se);"),
    "in a comment continued by a splice": main_with(
        "\t// still a comment \\\n\t(void)fuse_session_fd(se);"
    ),
    "excluded after an #undef": main_with(
        "#undef __APPLE__\n#ifdef __APPLE__\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "in a macro definition": main_with("#define GET_FD(s) fuse_session_fd(s)"),
    "in another function after main": (
        main_with("") + "static void f(void) { (void)fuse_session_fd(se); }\n"
    ),
    "if 0": main_with("#if 0\n\t(void)fuse_session_fd(se);\n#endif"),
    "excluded for macOS": main_with(
        "#ifndef __APPLE__\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "excluded by a spliced condition": main_with(
        "#if !defined(__CYGWIN__) && \\\n    !defined(__APPLE__)\n"
        "\t(void)fuse_session_fd(se);\n#endif"
    ),
    "garbage in an inactive region": main_with(
        "#if 0\n#if UNKNOWN_FUNCTION(\n\t(void)fuse_session_fd(se);\n#endif\n#endif"
    ),
}


@pytest.mark.parametrize("source", FLAGGED.values(), ids=list(FLAGGED))
def test_scanner_flags(source):
    assert names(source) != []


@pytest.mark.parametrize("source", ACCEPTED.values(), ids=list(ACCEPTED))
def test_scanner_accepts(source):
    assert names(source) == []


# Sources the scanner can't read must fail, not pass
UNSUPPORTED = {
    "unknown macro in #if": main_with(else_branch("#if SOME_UNKNOWN_MACRO")),
    "unknown macro in #ifdef": main_with(else_branch("#ifdef SOME_UNKNOWN_MACRO")),
    "unknown macro in #elif": main_with(
        "#if 0\n#elif SOME_UNKNOWN_MACRO\n#else\n\t(void)fuse_session_fd(se);\n#endif"
    ),
    "unknown function-like macro": main_with(
        else_branch("#if __has_include(<fuse.h>)")
    ),
    "unknown directive": main_with("#frobnicate\n"),
    "define without a value used in #if": main_with(
        "#define MY_FLAG\n" + else_branch("#if MY_FLAG")
    ),
    "prototype and parenthesized fuse_daemonize": (
        "int main(void) { res = fuse_mount(f, m); int fuse_daemonize(int); "
        "(void)fuse_session_fd(se); res = (fuse_daemonize)(0); }"
    ),
    "no fuse_mount": "int main(void) { res = fuse_daemonize(0); }",
    "no fuse_daemonize": "int main(void) { res = fuse_mount(f, m); }",
    "two fuse_daemonize": main_with("\tres = fuse_daemonize(0);"),
    "no main": "int f(void) { return 0; }",
    "unterminated main": "int main(void) { res = fuse_mount(f, m);",
    "main inside an unknown condition": (
        "#if SOME_UNKNOWN_MACRO\nint main(void) { res = fuse_mount(f, m); "
        "res = fuse_daemonize(0); }\n#endif\n"
    ),
}


@pytest.mark.parametrize("source", UNSUPPORTED.values(), ids=list(UNSUPPORTED))
def test_scanner_refuses(source):
    with pytest.raises(Unsupported):
        early_mount_triggers(source)


def test_no_mount_trigger_before_daemonize_on_macos():
    path = find_sshfs_c()
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        text = fh.read()
    try:
        early = early_mount_triggers(text)
    except Unsupported as exc:
        pytest.fail(f"{path}: {exc}; update test_daemonize_order.py", pytrace=False)
    assert not early, (
        "%s: in main(), after fuse_mount() and before fuse_daemonize(), a macOS "
        "build mentions %s. macFUSE 5.3.3+ starts the delayed mount on first use "
        "of the session, and fuse_daemonize() then stays in the foreground "
        "(upstream issues 373 and 388). Move it after fuse_daemonize() or "
        "exclude it with !defined(__APPLE__)."
        % (path, ", ".join(f"{name} (line {line})" for line, name in early))
    )
