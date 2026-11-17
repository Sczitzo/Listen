# Regression checks

From the repository root: `python tests/run_tests.py --no-devices`.
The 69 selected suites use disposable app data and block real network, devices,
physical input and clipboard access. This export includes the configured suites
and helpers, without private desktop trial recordings/results. The code-only
baseline.zip is required by latency_contract_test.py.

The physical logic_test.py suite is excluded by --no-devices. Do not run the
unrestricted command during another recording. Existing same-version Store
Python packages can be discovered automatically; packages are never installed
by the test runner. All 69 suites passed for this snapshot. Real cleanup remains unresolved; see the root README.
