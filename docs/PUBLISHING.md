# Release checklist (PyPI)

## Prerequisites

- PyPI project **`kiro-conduit`** exists under your account
- **Trusted publishing** configured on PyPI (required for `.github/workflows/release.yml`)

### Configure PyPI trusted publisher (one-time)

On [pypi.org](https://pypi.org) → **Your projects** → `kiro-conduit` → **Publishing** → **Add a new pending publisher**:

| Field | Value |
|-------|-------|
| PyPI project name | `kiro-conduit` |
| Owner | `walterwang0x01` |
| Repository name | `lwa-conduit` |
| Workflow name | `release.yml` |
| Environment name | *(leave empty — workflow does not use `environment:`)* |

Save, then re-run the failed Release workflow or create a new `v0.1.0` release.

**Verify claims** (from a failed run): `repository=walterwang0x01/lwa-conduit`, `workflow_ref=.../release.yml@refs/tags/v0.1.0`.

### Alternative: API token (manual)

If trusted publishing is not set up:

```bash
python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-... python -m twine upload dist/*
```

## Publish 0.1.x

1. Ensure `main` is green (CI: ruff, mypy, pytest).
2. Update `CHANGELOG.md` and `pyproject.toml` version if not already bumped.
3. Create a GitHub Release with tag `v0.1.0` (must match `project.version`).
4. Workflow `.github/workflows/release.yml` builds sdist/wheel and uploads to PyPI.

Manual dry-run locally:

```bash
python -m pip install build
python -m build
python -m twine check dist/*
```

## User install paths

```bash
# Recommended
pipx install kiro-conduit

# Or venv
pip install kiro-conduit

# From source
pip install 'git+https://github.com/walterwang0x01/lwa-conduit.git@v0.1.0'
```

Verify:

```bash
kiro-conduit --help
kiro-conduit report --quota-only
```
