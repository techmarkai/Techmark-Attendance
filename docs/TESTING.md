# Testing

Two suites, with different requirements.

## Pure tests — run anywhere

`tests/test_hr.py`, `test_isapi.py`, `test_no_unsupported_claims.py`,
`test_report.py`, `test_roster.py`, `test_stats.py`, `test_store.py`,
`test_stream.py`.

These need only `pytest`, `pytest-asyncio`, `httpx` and `openpyxl`. They run on
Windows, macOS or Linux and never touch a camera or a Home Assistant runtime.

```
python -m pytest tests/ -q
```

`tests/conftest.py` registers `custom_components.techmark_attendance` as a stub
module with an explicit `__path__` when Home Assistant is not installed, so the
pure modules import without dragging in the runtime.

## Runtime tests — need Linux and Python 3.14

`tests/test_runtime_setup.py`, `tests/test_runtime_flow.py`.

These drive a real `hass` object through `pytest-homeassistant-custom-component`
and execute `__init__`, `coordinator`, `config_flow`, `sensor`,
`binary_sensor` and `diagnostics`. They skip themselves (via
`pytest.importorskip`) when the harness is absent, so the command above stays
green without it.

**They cannot run on Windows.** `homeassistant/runner.py` imports `fcntl`,
which does not exist there. Home Assistant also requires Python 3.14 from
release 2026.3 onward.

The repository is developed on Windows, so the runtime suite runs in a
container:

```sh
docker run -d --name tma -v "$PWD:/repo" python:3.14-slim sleep infinity
docker exec tma pip install pytest-homeassistant-custom-component openpyxl
docker exec tma bash -lc 'cd /repo && python -m pytest tests/ -q'
```

The harness pins its own Home Assistant version, which trails the current
release by about a month. Check what you actually tested against:

```sh
docker exec tma python -c "from homeassistant.const import __version__ as v; print(v)"
```

## Two things worth knowing before you trust a green run

**A passing setup test does not prove thread safety.** sqlite3 binds a
connection to its creating thread. The bug that broke the first real install
was store calls dispatched on `hass.async_add_executor_job`, which picks an
arbitrary thread from Home Assistant's shared pool. Reintroduce that bug and
`test_setup_succeeds` still passes, because the harness serves sequential jobs
from one idle pool thread. Only
`test_concurrent_store_calls_stay_on_one_thread` catches it, by issuing the
calls at once and forcing the pool to use several threads — and the AST guard
in `test_no_unsupported_claims.py` catches it statically. Keep both.

**The guard tests assert over source text, not behaviour.** That is
deliberate: the risk they address is a misleading label — a column header or
sensor name that claims something the camera cannot support — which a
behavioural test would happily pass. If a change trips one of them, argue with
it rather than quietly satisfying it.

## Linting

```
python -m ruff check .
```

Configured in `pyproject.toml`. The settings are a contract, not taste: same
verdict for everyone.
