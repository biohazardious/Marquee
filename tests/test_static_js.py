"""Every front-end module has to at least parse.

`node --check` treats a .js file as a classic script, where `import` is a syntax
error it happens not to report -- so a genuinely broken module passed that check
while the browser refused to load it. Copying to .mjs first makes node parse it as
the module it is, which is what catches an unbalanced bracket.
"""
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "marquee", "web", "static")
JS = os.path.join(STATIC, "js")

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def modules():
    return sorted(name for name in os.listdir(JS) if name.endswith(".js"))


def test_the_page_has_modules_to_load():
    assert "app.js" in modules()


@node
@pytest.mark.parametrize("name", modules())
def test_module_parses(name, tmp_path):
    copy = tmp_path / (name[:-3] + ".mjs")
    with open(os.path.join(JS, name), encoding="utf-8") as handle:
        copy.write_text(handle.read(), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(copy)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"


@node
def test_every_import_resolves_to_a_file_that_exists():
    """A typo in a relative import fails silently until the page is opened."""
    import re
    pattern = re.compile(r"""from\s+['"](\./[^'"]+)['"]""")
    for name in modules():
        with open(os.path.join(JS, name), encoding="utf-8") as handle:
            for target in pattern.findall(handle.read()):
                assert os.path.isfile(os.path.join(JS, target)), \
                    f"{name} imports {target}, which does not exist"


def test_the_page_only_imports_modules_that_exist():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as handle:
        page = handle.read()
    import re
    for src in re.findall(r'<script[^>]+src="/static/([^"]+)"', page):
        assert os.path.isfile(os.path.join(STATIC, src)), f"index.html loads {src}"
    for href in re.findall(r'<link[^>]+href="/static/([^"]+)"', page):
        assert os.path.isfile(os.path.join(STATIC, href)), f"index.html loads {href}"


def _blanked(run):
    """A run of source replaced by spaces, keeping its newlines.

    Without this a block comment collapsed into one line and every line number after
    it was wrong -- which in a failure message is the only part anybody reads.
    """
    return "".join("\n" if char == "\n" else " " for char in run)


def _starts_a_regex(seen):
    """Is the `/` at this point a regex literal rather than a division?

    The usual heuristic: after a value a slash divides, after an operator or the
    start of an expression it opens a pattern. Good enough for this codebase, and the
    alternative is a JavaScript parser.
    """
    tail = seen.rstrip()
    if not tail:
        return True
    if tail[-1] in "(,=:[!&|?{};+-*%~^<>":
        return True
    word = re.findall(r"[A-Za-z_$][\w$]*$", tail)
    return bool(word) and word[0] in {"return", "typeof", "case", "in", "of", "new",
                                      "delete", "void", "instanceof", "do", "else",
                                      "yield", "await"}


def _strip_literals(text):
    """The source with every string, template, comment and regex blanked out.

    Brace and paren counting is only reliable once a `text: 'a (b'` cannot be
    mistaken for an unbalanced call -- and a regex holding a backtick cannot be
    mistaken for the start of a template literal, which is what `/[&*/:`<>?\\|"]/g`
    in util.js did: everything after it, half the module, went unchecked.
    """
    out = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        pair = text[index:index + 2]
        if pair == "//":
            end = text.find("\n", index)
            end = length if end < 0 else end
        elif pair == "/*":
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
        elif char in "'\"`":
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == char:
                    end += 1
                    break
                end += 1
        elif char == "/" and _starts_a_regex("".join(out)):
            end, inside = index + 1, False
            while end < length and text[end] != "\n":
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == "[":
                    inside = True
                elif text[end] == "]":
                    inside = False
                elif text[end] == "/" and not inside:
                    end += 1
                    break
                end += 1
        else:
            out.append(char)
            index += 1
            continue
        out.append(_blanked(text[index:end]))
        index = end
    return "".join(out)


def test_nothing_appends_a_bare_conditional_to_a_node():
    """`node.append(cond ? thing : null)` puts the word "null" on the page.

    Node.append() stringifies whatever is not a node, so a conditional child that
    resolves to null renders as literal text. `el()` filters those out of its own
    children and `append()` from util.js does the same for an existing node -- but
    calling `.append()` directly does not, and the folder picker showed a stray
    "null" under its buttons for exactly this reason.
    """
    import re
    offenders = []
    for name in modules():
        if name == "util.js":                       # where the filtering lives
            continue
        with open(os.path.join(JS, name), encoding="utf-8") as handle:
            source = handle.read()
        bare = _strip_literals(source)
        for match in re.finditer(r"\.append\(", bare):
            depth, index = 1, match.end()
            while index < len(bare) and depth:
                depth += {"(": 1, ")": -1}.get(bare[index], 0)
                index += 1
            arguments = bare[match.end():index - 1]
            # Only this call's own arguments: a nested el(...) filters its children.
            while True:
                # "@", not "()": leaving the parens in place stops the call that
                # contains them from folding, and the scan gives up one level too soon.
                folded = re.sub(r"\([^()]*\)", "@", arguments)
                if folded == arguments:
                    break
                arguments = folded
            if re.search(r":\s*null\s*(,|$)", arguments):
                line = bare.count("\n", 0, match.start()) + 1
                offenders.append(f"{name}:{line}")
    assert not offenders, ("use append() from util.js instead of .append(): "
                           + ", ".join(offenders))


def test_a_lazy_image_is_never_hidden_with_display_none():
    """It would never load, and so never become visible.

    A lazily-loaded <img> with `display: none` has no layout box, so it is never in
    the viewport, so the browser never fetches it and `load` never fires -- and the
    handler that would have shown it never runs. Every poster in the library showed
    its fallback initials for exactly this reason.
    """
    with open(os.path.join(JS, "util.js"), encoding="utf-8") as handle:
        source = handle.read()
    body = source[source.index("export function shot("):source.index("export function hero(")]
    assert "loading: 'lazy'" in body
    assert "display = 'none'" not in body


def test_the_settings_form_never_guesses_what_it_has_not_loaded():
    """A field that is not on screen must be omitted, not invented.

    values() used to fall back to the last adopted config, which reads as the same
    thing right up until that config has not arrived -- and then every field reported
    "" and one Save cleared the download client, its credentials and all three paths.
    JSON.stringify drops an undefined value, so omitting is the safe answer.
    """
    with open(os.path.join(JS, "settings.js"), encoding="utf-8") as handle:
        source = handle.read()
    body = source[source.index("export function values()"):source.index("function field(")]
    assert "current[id]" not in body, "values() is guessing from the adopted config"
    assert "=== undefined) delete" in body, "undefined keys must be dropped"
    # And the form is not drawn at all until something has been adopted.
    assert "current === null" in source


# Globals a browser provides, plus the keywords a "name(" regex cannot tell from a
# call. Anything else that is called and never declared does not exist at run time.
BROWSER = {
    "document", "window", "location", "history", "console", "fetch", "setTimeout",
    "clearTimeout", "setInterval", "clearInterval", "localStorage", "sessionStorage",
    "URLSearchParams", "Promise", "Set", "Map", "Object", "Array", "JSON", "Math",
    "Number", "String", "Boolean", "Date", "Error", "RegExp", "parseInt", "parseFloat",
    "isNaN", "encodeURIComponent", "decodeURIComponent", "alert", "confirm",
    "performance", "requestAnimationFrame", "Event", "CustomEvent", "AbortController",
    "getComputedStyle", "structuredClone", "queueMicrotask", "Intl", "Symbol",
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "await",
    "new", "else", "do", "try", "finally", "throw", "case", "of", "in", "delete",
    "async", "yield", "void", "instanceof",
}


def _declared(source):
    """Every name this module defines, imports or takes as a parameter.

    Deliberately generous: a name missed here is a false alarm, and a false alarm in a
    test that guards against typos is how the test gets deleted.
    """
    import re
    names = set(re.findall(r"(?:async\s+)?function\s+(\w+)", source))
    names |= set(re.findall(r"(?:const|let|var|class)\s+(\w+)", source))
    for block in re.findall(r"import\s*\{([^}]*)\}\s*from", source):
        names |= {part.strip().split(" as ")[-1].strip() for part in block.split(",")}
    names |= set(re.findall(r"import\s+(\w+)\s+from", source))
    names |= set(re.findall(r"import\s*\*\s*as\s+(\w+)\s+from", source))
    # `catch (error)` binds a name for the length of the block.
    names |= set(re.findall(r"catch\s*\(\s*(\w+)\s*\)", source))
    for block in re.findall(r"(?:const|let|var)\s*\[([^\]]*)\]", source):
        names |= {part.strip() for part in block.split(",") if part.strip().isidentifier()}

    # Parameter lists, both shapes, and destructured bindings.
    blocks = (re.findall(r"\(([^()]*)\)\s*=>", source)
              + re.findall(r"function\s*\w*\s*\(([^()]*)\)", source)
              + re.findall(r"(?:const|let|var)\s*\{([^}]*)\}", source))
    for block in blocks:
        for part in re.split(r"[,{}]", block):
            part = part.strip().split("=")[0].split(":")[-1].strip().lstrip(".")
            if part.isidentifier():
                names.add(part)
    # A single parameter needs no parentheses: `x => x.name`.
    names |= set(re.findall(r"(?:^|[^\w$.])(\w+)\s*=>", source, re.M))
    return {name for name in names if name}


def test_every_function_a_module_calls_actually_exists():
    """`node --check` parses; it does not look up names.

    A function deleted by a careless edit leaves a module that parses perfectly and
    throws "treeNodes is not defined" the moment the page draws. That happened, and the
    whole Selection tree came up empty with nothing in the suite to notice.
    """
    import re
    call = re.compile(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(")
    missing = []
    for name in modules():
        with open(os.path.join(JS, name), encoding="utf-8") as handle:
            source = _strip_literals(handle.read())
        known = _declared(source) | BROWSER
        for match in call.finditer(source):
            called = match.group(1)
            if called in known:
                continue
            line = source.count("\n", 0, match.start()) + 1
            missing.append(f"{name}:{line} {called}()")
    assert not missing, "called but never defined:\n" + "\n".join(sorted(set(missing)))


def test_nothing_reads_a_property_off_a_name_the_module_does_not_have():
    """`state.data` when `state` was never imported.

    The sibling test above only looks at call sites, so a missing import used purely
    as an object -- which is how every one of these modules uses `api` and `state` --
    slipped straight past it and threw on first render.
    """
    import re
    member = re.compile(r"(?<![.\w$?])([A-Za-z_$][\w$]*)\s*\.")
    missing = []
    for name in modules():
        with open(os.path.join(JS, name), encoding="utf-8") as handle:
            source = _strip_literals(handle.read())
        known = _declared(source) | BROWSER
        for match in member.finditer(source):
            read = match.group(1)
            if read in known:
                continue
            line = source.count("\n", 0, match.start()) + 1
            missing.append(f"{name}:{line} {read}.…")
    assert not missing, "read but never defined:\n" + "\n".join(sorted(set(missing)))
