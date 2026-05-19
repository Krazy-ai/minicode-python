# archive/

This directory holds historical files that used to live in the project root.
They are kept for reference but are **not** part of the runtime code or the
active test suite.

## Layout

| Subdirectory | Contents |
|--------------|----------|
| `reports/` | Iteration audit / optimization / security reports and old guides (19 markdown files, e.g. `FINAL_AUDIT_REPORT.md`, `CODE_WIKI.md`, `USAGE_GUIDE.md`). |
| `legacy-tests/` | Pre-`tests/` manual smoke / integration / visual scripts (`smoke_test.py`, `visual_test.py`, `test_integration.py`, `bench_optim.py`, etc.). They were already excluded from pytest collection via `conftest.py`. |
| `logs/` | One-off run outputs (`out.txt`, `session-log.txt`, `test_results.txt`). |
| `images/` | Hash-named PNG screenshots / illustrations from earlier docs. |
| `bundles/` | Source archives kept for reference (`claude-code.zip`, `minicode-py-src.tar.gz`). |
| `trae-notes/` | Salvaged design notes from the former `.trae/` directory: `specs/` (full spec folders for `optimize-memory-testing` and `evolve-agent-loop-intelligence`) and `documents/` (multi-agent orchestration / reflection plans). |
| `workbuddy-memory/` | Salvaged memory note from the former `.workbuddy/memory/` directory. |
| `misc/` | Anything that does not fit the categories above. |

## Notes

- Nothing here is imported by the `minicode` package.
- Safe to delete entire subdirectories if you do not need the history.
- If you reintroduce any script, move it back to the appropriate top-level
  directory (`tests/`, `benchmarks/`, `docs/`).
