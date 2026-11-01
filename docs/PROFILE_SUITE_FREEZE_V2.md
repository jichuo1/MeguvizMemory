# Profile-bound v2 suite preparation

`tools/freeze_device_suite_profile_v2.py` is a separate offline entry point. It
does not contact a device, issue admission, execute a pilot, qualify
performance, or select BIL. Keep its inputs, resulting protocol and plan, and
all collection output in private, ignored `build/` directories.

It requires a fresh PASS from the profile-aware functional runner, its same-run
`deployment.json`, the original owner admission, the independently held
profile SHA-256, and the exact DEX and 1080 image paths recorded in that run's
private manifest. The newer `input_binding` and `private_runtime_tools.json`
are mandatory; older receipts are rejected. Each of the eight profile roles is
matched by role, filename, local byte count and hash, and the functional
deployment's device hash. Both input paths, identities, the image oracle,
device serial and boot identity, and admission expiry must agree. Profile,
selected runtime tools and declared source identities are rechecked at freeze;
the private tool manifest includes machine-specific paths and must not be
published.

The result uses a fresh experiment identity, has four pilot then twelve formal
windows, includes both families and their four variants, and never pools old
samples or enables retries. The plan is exactly `make_plan(protocol)`; all
qualification flags remain false. It is a preparation artifact, not evidence
of a real device run or performance benefit. A failed or incomplete functional
run cannot produce this freeze.

`tools/run_device_suite_profile_v2.py` is the profile-aware capture entry point.
Before its first ADB command it revalidates the frozen protocol and exact plan,
profile and candidate sources, the functional receipt and deployment, input
and artifact identities, admission and device binding, and the ADB and Python
file identities recorded by the functional run. It also checks the collector's
separate performance deployment locally before a pilot or formal run, and
recomputes the pilot gates before admitting formal work. Output must use a new
directory under private `build/`; local inputs are checked for unsafe paths,
links, and hard links. The profile-aware entry inherits the existing v2
collector for actual device operations and remote artifact verification.

The plain `tools/run_device_suite_v2.py` collector does not provide these extra
profile-aware checks. Source closure is checked at the start and completion of
a capture, not continuously; the tool executables are not rehashed before
every command. The inherited collector applies command timeouts, but buffers
subprocess output without a hard byte limit. These checks are local integrity
evidence, not signed provenance, a shared device lock, or proof of the actual
mapped executable bytes. Timeouts and interrupts do not establish that a
remote process has exited.

The functional `deployment.json` records provenance only. The profile-aware
collector must separately create and verify its own `STAGED` performance
deployment; the functional deployment is not a substitute. Capture still
requires a current exclusive device window, correct boot identity, valid
admission and the unexpired frozen schedule. Admission and observational
occupancy checks do not reserve the device against another session.

`tools/test_freeze_device_suite_profile_v2.py` and
`tools/test_run_device_suite_profile_v2.py` exercise these preparation and
capture gates with synthetic inputs offline. They do not invoke ADB or establish
that staging, pilot, formal sampling, or performance qualification succeeded
on a device. No device run is established by these offline checks.
