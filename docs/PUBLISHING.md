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
   - `release.published` event: build + attach assets + publish to PyPI
   - `workflow_dispatch` default: build only
   - `workflow_dispatch` with `publish_to_pypi=true`: build + publish
5. Even if PyPI publish fails, the workflow now uploads `dist/*` as:
   - a GitHub Actions artifact: `kiro-conduit-dist`
   - GitHub Release assets on the tag release

This means trusted publishing misconfiguration no longer blocks users from downloading
the built wheel/sdist or maintainers from doing a later manual `twine upload`.

Manual dry-run locally:

```bash
python -m pip install build
python -m build
python -m twine check dist/*
```

Manual dry-run in GitHub Actions:

1. Open Actions → `Release`
2. Click `Run workflow`
3. Leave `publish_to_pypi` unchecked to only build artifacts

Equivalent CLI commands:

```bash
# Build-only dry-run
gh workflow run release.yml -f publish_to_pypi=false

# Build + publish (after trusted publisher is configured)
gh workflow run release.yml -f publish_to_pypi=true
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

## If PyPI still fails

1. Open the failed `Release` workflow run
2. Download artifact `kiro-conduit-dist`, or fetch the files from the GitHub Release assets
3. After fixing trusted publishing, either:
   - re-run the workflow, or
   - upload the saved `dist/*` manually with `twine`
