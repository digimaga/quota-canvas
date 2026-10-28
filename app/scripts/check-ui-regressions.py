#!/usr/bin/env python3
"""Guard the view wiring that cannot be exercised without launching a GUI."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
display = (root / 'Sources/QuotaCanvasApp/Views/SettingsView.swift').read_text()
routing = (root / 'Sources/QuotaCanvasApp/Views/RoutingSettingsView.swift').read_text()
en = (root / 'Resources/en.lproj/Localizable.strings').read_text()
checks = {
    'engine settings are outside the config-dependent form':
        display.index('.safeAreaInset(edge: .bottom) { Form { engineSection }') > display.index('} else if loadFailed'),
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
