"""Lexical sanity check for Kotlin sources — brace and comment balance.

A cheap pre-flight, from when this machine had no JDK or Android SDK and
nothing here could be type-checked at all. It now can (`./gradlew
:app:compileReleaseKotlin`), so this is the ten-millisecond answer rather than
the only one — worth keeping for exactly the mistakes it was written for,
which the compiler reports far away from their cause:

  - **Nested block comments.** Kotlin nests them, unlike Java. A `/*` inside a
    KDoc (writing a MIME wildcard such as image-slash-star, say) opens a second
    comment, and the KDoc's closing delimiter then only closes the inner one.
    Everything after it is swallowed until some unrelated `*/` turns up.
  - Unterminated strings, and braces that do not balance.

Run `python kt_lex.py --self-test` to check the scanner against sources whose
answer is known. It is worth having because this tool's failure mode is a
**false** alarm: it once called a file broken that compiled perfectly, having
read the apostrophe in a backtick test name as an unterminated char literal
and swallowed the rest of the file. A checker that cries wolf gets ignored,
which costs more than not having one.

Usage: python kt_lex.py <root>
"""
import sys
import os

NORMAL, LINE, BLOCK, STR, RAW, CHAR, TICK = range(7)


def scan(src):
    """(errors, brace_depth). Walks the file as the lexer would."""
    errors = []
    state = NORMAL
    depth = 0            # block-comment nesting
    braces = 0
    comment_start = None
    str_start = None
    tick_start = None
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if c == '\n':
            line += 1
            if state == LINE:
                state = NORMAL
            elif state == TICK:
                # A backtick identifier cannot span a line. Saying so here,
                # rather than running to the end of the file looking for a
                # partner, keeps one stray backtick from condemning everything
                # under it — which is how this scanner's own false alarm read.
                errors.append(f'line {tick_start}: backtick identifier not '
                              f'closed before the end of the line')
                state = NORMAL
            i += 1
            continue
        if state == NORMAL:
            if c == '/' and nxt == '/':
                state = LINE; i += 2; continue
            if c == '/' and nxt == '*':
                state, depth, comment_start = BLOCK, 1, line; i += 2; continue
            if src.startswith('"""', i):
                state, str_start = RAW, line; i += 3; continue
            if c == '"':
                state, str_start = STR, line; i += 1; continue
            if c == "'":
                state, str_start = CHAR, line; i += 1; continue
            if c == '`':
                # `fun \`a quick trip does not ask again\`()`. Everything in
                # here is a name, so an apostrophe in it is a letter — reading
                # it as a char literal is what swallowed a whole test file.
                state, tick_start = TICK, line; i += 1; continue
            if c == '{':
                braces += 1
            elif c == '}':
                braces -= 1
                if braces < 0:
                    errors.append(f'line {line}: closing brace with nothing open')
                    braces = 0
            i += 1; continue
        if state == BLOCK:
            if c == '/' and nxt == '*':
                depth += 1
                errors.append(
                    f'line {line}: "/*" inside a block comment opened on line '
                    f'{comment_start} — Kotlin NESTS these, so the closing '
                    f'delimiter will only close this inner one')
                i += 2; continue
            if c == '*' and nxt == '/':
                depth -= 1
                if depth == 0:
                    state = NORMAL
                i += 2; continue
            i += 1; continue
        if state == LINE:
            i += 1; continue
        if state == RAW:
            if src.startswith('"""', i):
                state = NORMAL; i += 3; continue
            i += 1; continue
        if state == TICK:
            # No escapes inside one, so the next backtick ends it.
            if c == '`':
                state = NORMAL
            i += 1; continue
        # STR / CHAR
        if c == '\\':
            i += 2; continue
        if (state == STR and c == '"') or (state == CHAR and c == "'"):
            state = NORMAL
        i += 1
    if state == BLOCK:
        errors.append(f'block comment opened on line {comment_start} never closed')
    if state in (STR, RAW, CHAR):
        errors.append(f'string opened on line {str_start} never closed')
    if state == TICK:
        # The newline branch above catches these everywhere except on the last
        # line of a file that ends without one — which is where the self-test
        # found this missing.
        errors.append(f'line {tick_start}: backtick identifier never closed')
    return errors, braces


# --- does the scanner agree with the compiler ---------------------------------
# Each case is (name, source, should_it_complain). The clean ones matter most:
# every false alarm this tool has produced was a valid construct it had never
# been shown.
CASES = [
    ("plain", 'fun a() { val x = "hi" }', False),
    ("backtick name with an apostrophe",
     'class T { @Test fun `the app\'s own picker`() { val n = 1 } }', False),
    ("backtick name with braces and quotes in it",
     'fun `a {name} with "quotes"`() { }', False),
    ("char literal still works", "fun a() { val c = '}' }", False),
    ("apostrophe in a line comment", "fun a() { } // don't count this", False),
    ("apostrophe in a string", 'fun a() { val s = "don\'t" }', False),
    ("apostrophe in a KDoc", "/** don't */\nfun a() { }", False),
    ("raw string holding a brace", 'fun a() { val s = """{"""  }', False),
    ("escaped quote in a string", 'fun a() { val s = "she said \\"hi\\"" }', False),
    # ...and the things it exists to catch.
    ("nested block comment", "/* outer /* inner */\nfun a() { }", True),
    ("unterminated string", 'fun a() { val s = "oops }', True),
    ("unclosed backtick", 'fun `oops() { }', True),
    ("stray closing brace", "fun a() { } }", True),
]


def self_test():
    bad = 0
    for name, src, should_fail in CASES:
        errs, braces = scan(src)
        if braces != 0:
            errs.append(f'braces {braces:+d}')
        failed = bool(errs)
        ok = failed == should_fail
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  <- ' + (str(errs) if failed else 'said nothing')}")
    print()
    if bad:
        print(f'{bad} FAILED')
        return 1
    print(f'the scanner agrees on all {len(CASES)} cases')
    return 0


if len(sys.argv) > 1 and sys.argv[1] == '--self-test':
    sys.exit(self_test())

root = sys.argv[1] if len(sys.argv) > 1 else '.'
files = []
for base, _, names in os.walk(root):
    files += [os.path.join(base, f) for f in names if f.endswith('.kt')]

bad = 0
for path in sorted(files):
    errs, braces = scan(open(path, encoding='utf-8').read())
    if braces != 0:
        errs.append(f'braces do not balance (depth {braces:+d} at end of file)')
    if errs:
        bad += 1
        print(f'FAIL {os.path.relpath(path, root)}')
        for e in errs:
            print(f'      {e}')

print(f'\n{len(files) - bad}/{len(files)} Kotlin files clean')
sys.exit(1 if bad else 0)
