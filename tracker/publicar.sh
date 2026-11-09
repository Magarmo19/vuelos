#!/usr/bin/env bash
# Guarda la carpeta datos/data en la rama "datos" como un único commit sin historia
# (así el repositorio no crece). Solo sube los archivos que han cambiado.
set -euo pipefail
cd "$(dirname "$0")/../datos"
git add -A data
tree=$(git write-tree)
if git rev-parse -q --verify refs/remotes/origin/datos >/dev/null \
   && [ "$tree" = "$(git rev-parse 'refs/remotes/origin/datos^{tree}')" ]; then
  echo "datos sin cambios"
  exit 0
fi
commit=$(git commit-tree "$tree" -m "datos $(date -u +%Y-%m-%dT%H:%M:%SZ)")
git push -q -f origin "$commit:refs/heads/datos"
git update-ref refs/remotes/origin/datos "$commit"
echo "datos publicados ($commit)"
