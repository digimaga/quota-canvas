#!/usr/bin/env python3
"""Guard the view wiring that cannot be exercised without launching a GUI."""
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
display = (root / 'Sources/QuotaCanvasApp/Views/SettingsView.swift').read_text()
routing = (root / 'Sources/QuotaCanvasApp/Views/RoutingSettingsView.swift').read_text()
en = (root / 'Resources/en.lproj/Localizable.strings').read_text()
app = (root / 'Sources/QuotaCanvasApp/QuotaCanvasApp.swift').read_text()


def swift_code_skeleton(text):
    """Comments and string literals blanked to spaces (positions preserved),
    so brace matching only sees real code."""
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
            blank(i, j + 3)
            i = j + 3
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
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


def engine_settings_outside_config_form():
    """engineSection must render via safeAreaInset outside the if/else
    chain on `doc`, so a failed config load still shows it."""
    marker = '.safeAreaInset(edge: .bottom) { Form { engineSection }'
    skeleton = swift_code_skeleton(display)
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
    except ValueError:
        return False
    return ('} else if loadFailed' in skeleton
            and inset > end
            and skeleton.count('engineSection') == 2)  # comments/strings excluded


checks = {
    'app starts the StateObject instance':
        '@StateObject private var statusStore: StatusStore' in app
        and 'let store = StatusStore()' in app
        and '_statusStore = StateObject(wrappedValue: store)' in app
        and 'store.start()' in app
        and 'let store = statusStore' not in app,
    'engine settings are outside the config-dependent form':
        engine_settings_outside_config_form(),
    'engine path can reload the config': 'Button("再読み込み") { Task { await load() } }' in display,
    'worker editor can append a row': 'rows.append(WorkerRow(name: "", windowsText: ""))' in routing,
    'routing polls while the view task is active':
        'while !Task.isCancelled' in routing and '60_000_000_000' in routing and 'await reloadVerdicts()' in routing,
    'routing freshness uses verdict timestamp': 'verdictTimestamp = snapshot?.routing?.at' in routing,
    'outdated color translation is removed': '超過・枯渇は赤、ギリギリは黄、余裕ありは緑。' not in en,
    'routing uses the segmented CLI mode picker':
        '.pickerStyle(.segmented)' in routing and 'client.routingModes()' in routing
        and 'client.setRoutingMode(name: name, mode: mode)' in routing
        and 'Text("監視のみ").tag("off")' in routing,
    'burn fallback has an English translation': '"使い切り" = "Burn";' in en,
}
for name, passed in checks.items():
    if not passed:
        raise SystemExit(f'FAIL: {name}')
print(f'View wiring checks: {len(checks)} passed')
