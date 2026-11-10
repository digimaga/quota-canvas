#!/usr/bin/env python3
"""Guard the view wiring that cannot be exercised without launching a GUI."""
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
display = (root / 'Sources/QuotaCanvasApp/Views/SettingsView.swift').read_text()
routing = (root / 'Sources/QuotaCanvasApp/Views/RoutingSettingsView.swift').read_text()
en = (root / 'Resources/en.lproj/Localizable.strings').read_text()
app = (root / 'Sources/QuotaCanvasApp/QuotaCanvasApp.swift').read_text()


def swift_code_skeleton(text, keep_literals=False):
    """Comments blanked to spaces (positions preserved). String literals are
    blanked too unless keep_literals, in which case they stay verbatim.
    Brace matching on the result only sees real code."""
    out = list(text)
    n = len(text)

    def blank(start, end):
        for k in range(start, min(end, n)):
            if out[k] != '\n':
                out[k] = ' '

    i = 0
    while i < n:
        if text.startswith('//', i):
            end = text.find('\n', i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif text.startswith('/*', i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text.startswith('/*', j):
                    depth += 1
                    j += 2
                elif text.startswith('*/', j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif text.startswith('"""', i):
            j = i + 3
            while j < n and not text.startswith('"""', j):
                j += 2 if text[j] == '\\' else 1
            if not keep_literals:
                blank(i, j + 3)
            i = j + 3
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            if not keep_literals:
                blank(i, j + 1)
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def matching_brace(skeleton, open_index):
    """Index of the '}' closing the '{' at open_index (skeleton has no
    braces inside comments or string literals)."""
    depth = 0
    for k in range(open_index, len(skeleton)):
        if skeleton[k] == '{':
            depth += 1
        elif skeleton[k] == '}':
            depth -= 1
            if depth == 0:
                return k
    raise ValueError('unbalanced braces')


def matching_paren(skeleton, open_index):
    """Index of the ')' closing the '(' at open_index."""
    depth = 0
    for k in range(open_index, len(skeleton)):
        if skeleton[k] == '(':
            depth += 1
        elif skeleton[k] == ')':
            depth -= 1
            if depth == 0:
                return k
    raise ValueError('unbalanced parens')


def code_pattern(code):
    """Regex matching `code` inside a skeleton: the pattern's string
    literals blank to spaces like real ones, and whitespace is allowed at
    every token boundary (\\s* around punctuation, \\s+ between words)."""
    toks = re.findall(r'\w+|[^\w\s]', swift_code_skeleton(code))
    word = lambda c: c.isalnum() or c == '_'
    out = []
    for i, t in enumerate(toks):
        if i:
            out.append(r'\s+' if word(t[0]) and word(toks[i - 1][-1])
                       else r'\s*')
        out.append(re.escape(t))
    return ''.join(out)


def has_code(skeleton, code):
    return re.search(code_pattern(code), skeleton) is not None


def enclosing_braces(skeleton, index):
    """'{' positions of every block still open at index."""
    opens = []
    for k in range(index):
        if skeleton[k] == '{':
            opens.append(k)
        elif skeleton[k] == '}' and opens:
            opens.pop()
    return opens


def modifier_chain(skeleton, pos):
    """Yield (name, arg_text) for each '.name(args)' / '.name { }' view
    modifier starting at pos; trailing and labeled closures are consumed."""
    while True:
        m = re.match(r'\s*\.\s*([A-Za-z_]\w*)', skeleton[pos:])
        if not m:
            labeled = re.match(r'\s*[A-Za-z_]\w*\s*:\s*\{', skeleton[pos:])
            if not labeled:
                return
            pos = matching_brace(skeleton, pos + labeled.end() - 1) + 1
            continue
        name = m.group(1)
        pos += m.end()
        arg = ''
        p = re.match(r'\s*\(', skeleton[pos:])
        if p:
            arg_open = pos + p.end() - 1
            arg_close = matching_paren(skeleton, arg_open)
            arg = skeleton[arg_open + 1:arg_close]
            pos = arg_close + 1
        b = re.match(r'\s*\{', skeleton[pos:])
        if b:
            pos = matching_brace(skeleton, pos + b.end() - 1) + 1
        yield name, arg


def const_true(arg):
    """The whole argument is the constant `true` (modulo parentheses), so
    '.disabled(<arg>)' is always off — unlike 'true && modeBusy'."""
    arg = arg.strip()
    while arg.startswith('('):
        try:
            if matching_paren(arg, 0) != len(arg) - 1:
                break
        except ValueError:
            break
        arg = arg[1:-1].strip()
    return arg == 'true'


def dead_modifier(skeleton, start, end):
    """A '.disabled(true)' or '.hidden()' trailing the expression ending at
    `end`, or trailing a block enclosing `start`, makes it unreachable."""
    try:
        positions = [end] + [matching_brace(skeleton, o) + 1
                             for o in enclosing_braces(skeleton, start)]
        for pos in positions:
            for name, arg in modifier_chain(skeleton, pos):
                if name == 'hidden' or (name == 'disabled'
                                        and const_true(arg)):
                    return True
    except ValueError:
        return True
    return False


def return_on_path(skeleton, index):
    """A 'return' before index at the top level of any block enclosing index
    (not only the innermost one), e.g. 'set: { return; if let n { f() } }'."""
    for o in enclosing_braces(skeleton, index):
        depth = 0
        segment = skeleton[o + 1:index]
        for m in re.finditer(r'\breturn\b|[{}]', segment):
            tok = m.group(0)
            if tok == '{':
                depth += 1
            elif tok == '}':
                depth -= 1
            elif depth == 0 and re.search(r'[;\n]', segment[m.end():]):
                # 'return; f()' or 'return\n f()'. 'return f()' (same
                # statement) still runs f, so it is not dead code.
                return True
    return False


def early_return_before(skeleton, index):
    """A 'return' at the same block level before index makes it dead code."""
    opens = enclosing_braces(skeleton, index)
    if not opens:
        return False
    depth = 0
    for m in re.finditer(r'\breturn\b|[{}]', skeleton[opens[-1] + 1:index]):
        tok = m.group(0)
        if tok == '{':
            depth += 1
        elif tok == '}':
            depth -= 1
        elif depth == 0:
            return True
    return False


def set_closure_calls(text, callee):
    """The first 'set:' closure in text must call `callee` — guards a
    Binding whose setter was disconnected."""
    try:
        m = re.search(r'\bset\s*:', text)
        set_body = text[text.index('{', m.end()):]
        set_body = set_body[:matching_brace(set_body, 0) + 1]
        # A real call (not just the name), not preceded by an unconditional
        # 'return' at the closure's top level ('return; setMode(...)').
        return any(not return_on_path(set_body, c.start())
                   for c in re.finditer(r'\b%s\s*\(' % re.escape(callee), set_body))
    except (ValueError, AttributeError):
        return False


def binding_setter_calls(skeleton, marker, callee):
    """The 'set:' closure inside the func/var body at `marker`."""
    try:
        found = re.search(code_pattern(marker), skeleton)
        if not found:
            return False
        start = found.start()
        body = skeleton[start:matching_brace(skeleton, skeleton.index('{', start))]
    except ValueError:
        return False
    return set_closure_calls(body, callee)


def off_option(source, skeleton):
    """A real `Text("監視のみ")` option whose modifier chain (any modifiers,
    e.g. .foregroundStyle(.secondary)) ends in `.tag("off")`. `source` keeps
    string literals and `skeleton` blanks them at the same offsets, so a
    match inside a (multi-line) string literal is rejected."""
    for m in re.finditer(r'Text\s*\(\s*"監視のみ"\s*\)', source):
        if not skeleton[m.start():m.start() + 4] == 'Text':
            continue  # inside a string literal, not code
        try:
            pos = m.end()
            while True:
                mod = re.match(r'\s*\.\s*([A-Za-z_]\w*)', skeleton[pos:])
                if not mod:
                    break
                pos += mod.end()
                paren = re.match(r'\s*\(', skeleton[pos:])
                if paren:
                    arg_open = pos + paren.end() - 1
                    arg_close = matching_paren(skeleton, arg_open)
                    if mod.group(1) == 'tag' and re.fullmatch(
                            r'\s*"off"\s*', source[arg_open + 1:arg_close]):
                        return True
                    pos = arg_close + 1
                brace = re.match(r'\s*\{', skeleton[pos:])  # trailing closure
                if brace:
                    pos = matching_brace(skeleton, pos + brace.end() - 1) + 1
        except ValueError:
            continue
    return False


def selection_routes_to_setmode(args, skeleton):
    """The Picker's 'selection:' argument is a Binding whose setter calls
    setMode — inline Binding(...) or the modeBinding(worker) helper."""
    m = re.search(r'\bselection\s*:', args)
    if not m:
        return False
    value = args[m.end():].strip().rstrip(',')
    if re.match(r'modeBinding\s*\(', value):
        return binding_setter_calls(skeleton, 'func modeBinding', 'setMode')
    if re.match(r'Binding\s*\(', value):
        return set_closure_calls(value, 'setMode')
    return False


def app_starts_status_store():
    m = re.search(code_pattern('store.start()'), app_skel)
    return (m is not None
            and not early_return_before(app_skel, m.start())
            and has_code(app_skel, '@StateObject private var statusStore: StatusStore')
            and has_code(app_skel, 'let store = StatusStore()')
            and has_code(app_skel, '_statusStore = StateObject(wrappedValue: store)')
            and not has_code(app_skel, 'let store = statusStore'))


def engine_settings_outside_config_form():
    """engineSection must render via safeAreaInset outside the if/else
    chain on `doc`, so a failed config load still shows it."""
    marker = '.safeAreaInset(edge: .bottom) { Form { engineSection }'
    skeleton = display_skel
    try:
        branch = skeleton.index('if let doc')
        # Walk the whole if / else if / else chain to its closing brace;
        # the inset must come after it, not inside any branch.
        end = matching_brace(skeleton, skeleton.index('{', branch))
        while True:
            m = re.match(r'[ \t\n]*else\b', skeleton[end + 1:])
            if not m:
                break
            end = matching_brace(skeleton, skeleton.index('{', end + 1 + m.end()))
        inset = skeleton.index(marker)
        inset_end = matching_brace(skeleton, skeleton.index('{', inset)) + 1
    except ValueError:
        return False
    return ('} else if loadFailed' in skeleton
            and inset > end
            and not dead_modifier(skeleton, inset, inset_end)
            and skeleton.count('engineSection') == 2)  # comments/strings excluded


def engine_path_can_reload():
    m = re.search(code_pattern('Button("再読み込み") { Task { await load() } }'),
                  display_skel)
    return m is not None and not dead_modifier(display_skel, m.start(), m.end())


def worker_editor_appends_row():
    m = re.search(code_pattern('rows.append(WorkerRow(name: "", windowsText: ""))'),
                  routing_skel)
    return m is not None and not dead_modifier(routing_skel, m.start(), m.end())


def routing_polls():
    """The poll loop must be live code: no same-block 'return' before the
    loop or before the verdict reload inside it."""
    m = re.search(code_pattern('while !Task.isCancelled'), routing_skel)
    if not m or early_return_before(routing_skel, m.start()):
        return False
    try:
        brace = routing_skel.index('{', m.end())
        body = routing_skel[brace:matching_brace(routing_skel, brace)]
    except ValueError:
        return False
    sleep = re.search(code_pattern('60_000_000_000'), body)
    reload = re.search(code_pattern('await reloadVerdicts()'), body)
    return bool(sleep and reload
                and not early_return_before(routing_skel, brace + reload.start()))


def routing_uses_segmented_picker():
    """Each segmented Picker's selection must be a Binding wired to setMode
    (not a cut setter or a constant), with a live 'off' tag."""
    try:
        pickers = []
        for m in re.finditer(r'\bPicker\s*\(', routing_skel):
            arg_open = m.end() - 1
            arg_close = matching_paren(routing_skel, arg_open)
            tail = re.match(r'\s*\{', routing_skel[arg_close + 1:])
            if not tail:
                continue
            label_open = arg_close + tail.end()
            label_close = matching_brace(routing_skel, label_open)
            chain = list(modifier_chain(routing_skel, label_close + 1))
            if any(n == 'pickerStyle' and re.search(r'\.\s*segmented\b', a)
                   for n, a in chain):
                pickers.append((m.start(),
                                routing_skel[arg_open + 1:arg_close],
                                (routing_source[label_open:label_close],
                                 routing_skel[label_open:label_close]),
                                label_close + 1))
    except ValueError:
        return False
    return (bool(pickers)
            and all(selection_routes_to_setmode(args, routing_skel)
                    and not dead_modifier(routing_skel, start, chain_at)
                    and off_option(labels, label_skel)
                    for start, args, (labels, label_skel), chain_at in pickers)
            and has_code(routing_skel, 'client.routingModes()')
            and has_code(routing_skel, 'client.setRoutingMode(name: name, mode: mode)'))


app_skel = swift_code_skeleton(app)
display_skel = swift_code_skeleton(display)
routing_skel = swift_code_skeleton(routing)
routing_source = swift_code_skeleton(routing, keep_literals=True)

checks = {
    'app starts the StateObject instance': app_starts_status_store(),
    'engine settings are outside the config-dependent form':
        engine_settings_outside_config_form(),
    'engine path can reload the config': engine_path_can_reload(),
    'worker editor can append a row': worker_editor_appends_row(),
    'routing polls while the view task is active': routing_polls(),
    'routing freshness uses verdict timestamp':
        has_code(routing_skel, 'verdictTimestamp = snapshot?.routing?.at')
        and len(re.findall(r'verdictTimestamp\s*=(?!=)', routing_skel)) == 1,
    'outdated color translation is removed':
        '超過・枯渇は赤、ギリギリは黄、余裕ありは緑。' not in en,
    'routing uses the segmented CLI mode picker': routing_uses_segmented_picker(),
    'burn fallback has an English translation': '"使い切り" = "Burn";' in en,
}
for name, passed in checks.items():
    if not passed:
        raise SystemExit(f'FAIL: {name}')
print(f'View wiring checks: {len(checks)} passed')
