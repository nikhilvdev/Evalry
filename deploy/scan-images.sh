#!/usr/bin/env bash
# Scan the images Evalry runs for known vulnerabilities (security review F14). Not run by Claude: it pulls a scanner image and the
# public vulnerability database, so run it yourself, on a machine you trust, and keep the output with the review evidence.
#
#   TRIVY_IMAGE=aquasec/trivy@sha256:<digest you reviewed> deploy/scan-images.sh
#
# The scanner image must be pinned by digest (a moved tag would change what "clean" means). Exit 1 means at least one HIGH or
# CRITICAL vulnerability that has a fix; read the report, then upgrade the image (and its pinned digest) or record the risk.
set -euo pipefail
: "${TRIVY_IMAGE:?set TRIVY_IMAGE to a digest-pinned scanner image, for example aquasec/trivy@sha256:...}"
case "$TRIVY_IMAGE" in *@sha256:*) ;; *) echo "TRIVY_IMAGE must be pinned by digest (name@sha256:...)" >&2; exit 2 ;; esac

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/.."   # the repository root, so the paths below resolve wherever the script is run from
# The images that run untrusted code or hold keys: the sandbox image, and the bases of the generator and judge image.
images=("$(python3 - <<'PY'
import re, pathlib
print(re.search(r'PYTHON_IMAGE = "([^"]+)"', pathlib.Path("src/evalry/images.py").read_text()).group(1))
PY
)")
while read -r ref; do images+=("$ref"); done < <(grep -E '^FROM ' "$here/Dockerfile.generator" | awk '{print $2}')
images+=("$(grep -E 'image: postgres' "$here/docker-compose.yml" | awk '{print $2}')")

status=0
for image in "${images[@]}"; do
  echo "=== $image"
  # Scanning straight from the registry means the scanner never needs the Docker socket.
  docker run --rm -v evalry-trivy-cache:/root/.cache/ "$TRIVY_IMAGE" image --image-src remote --scanners vuln \
    --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image" || status=1
done
exit "$status"
