# Regression checks

From the repository root: `python tests/run_tests.py --no-devices`.
The 72 selected suites use disposable app data and block real network, devices,
physical input and clipboard access. This export includes the configured suites
and helpers, without private desktop trial recordings/results. The code-only
baseline.zip is required by latency_contract_test.py.

The physical logic_test.py suite is excluded by --no-devices. Do not run the
unrestricted command during another recording. Existing same-version Store
Python packages can be discovered automatically; the runner never installs them.

`preview_card_windows_test.py --real-windows --receipt <new-file>` opens temporary
native windows with synthetic text. Request owner readiness first. It uses an
owned editor and native wheel messages; the additional physical-scrollbar option
is separate opt-in. It does not record audio or access the clipboard.

Lantern coverage: preview_card_test.py and preview_shadow_test.py check voice response, silence/stale input, teardown, shadow pixels and native-resource failure paths without devices.
