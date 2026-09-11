# Maintainer constraints

`ci.txt` is a universal Python 3.11+ resolution of all SDK extras and maintainer
tools. `tooling.in` pins the audit/build tools; consumer ranges remain in
`pyproject.toml`. Regenerate using the command in the generated header and review
all version/marker changes. CI uses constraints for PR/release checks and resolves
fresh ranges on its weekly schedule. Dependabot must include this directory.
