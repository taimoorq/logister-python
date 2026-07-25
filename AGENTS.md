# Logister Python SDK Agent Notes

This is a public package repository. Never commit credentials, private
telemetry, customer data, or artifacts containing local configuration.

## Dependency maintenance

- `pyproject.toml` is the source of truth for the package version, Python floor,
  runtime dependencies, framework extras, and development dependencies.
- Keep classifiers and CI aligned with `requires-python`; CI currently covers
  Python 3.11 through 3.14.
- Preserve bounded framework majors where compatibility matters, and test all
  declared extras together before widening a range.
- Keep pip and GitHub Actions Dependabot updates enabled. Pin Actions to full
  commit SHAs and retain a readable version comment.

## Verification

Run before handoff or release:

```bash
python -m pip install --upgrade pip 'setuptools>=83'
python -m pip install -e '.[dev,fastapi,celery,django,flask]' pip-audit
python -m pip_audit
python -m pytest
python -m build
```

## Release contract

- Update `pyproject.toml` and `CHANGELOG.md` together.
- Merging a new version to `main` runs CI, creates `vX.Y.Z`, and explicitly
  dispatches `.github/workflows/publish.yml`. Keep the explicit dispatch because
  tags pushed with `GITHUB_TOKEN` do not start tag-push workflows.
- PyPI trusted publishing is bound to the repository, the `pypi` environment,
  and the workflow filename `publish.yml`. Renaming that workflow requires the
  PyPI publisher configuration to be updated first.
- Keep release ordering as build once, upload the exact distributions to PyPI,
  then create the GitHub Release. A manual recovery may dispatch `publish.yml`
  from `main` with an existing `tag` input so the trusted-publisher identity
  remains `publish.yml@main` while the build checks out the tag.
- PyPI versions are immutable. Never reuse an accepted version.
- Verify both surfaces before calling a release complete:

```bash
curl -fsSL https://pypi.org/pypi/logister-python/json | jq -r .info.version
gh release view vX.Y.Z
```
