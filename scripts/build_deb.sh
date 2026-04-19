#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.1.0}"
PKG_NAME="ai-os"
ARCH="all"

BUILD_ROOT="${ROOT_DIR}/build"
DIST_DIR="${ROOT_DIR}/dist"
STAGE_DIR="${BUILD_ROOT}/${PKG_NAME}_${VERSION}"
PKG_DIR="${STAGE_DIR}/${PKG_NAME}_${VERSION}"
OUT_DEB="${DIST_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb"

rm -rf "${STAGE_DIR}"
mkdir -p \
  "${PKG_DIR}/DEBIAN" \
  "${PKG_DIR}/opt/ai-os" \
  "${PKG_DIR}/usr/bin" \
  "${PKG_DIR}/usr/share/applications" \
  "${PKG_DIR}/usr/share/doc/${PKG_NAME}" \
  "${DIST_DIR}"

cp -r "${ROOT_DIR}/ai_os/ai_os" "${PKG_DIR}/opt/ai-os/ai_os"
cp "${ROOT_DIR}/README.md" "${PKG_DIR}/usr/share/doc/${PKG_NAME}/README.md"

cat > "${PKG_DIR}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: AI OS Team <ai-os@example.com>
Depends: python3, python3-tk, python3-pyaudio, python3-pip, espeak-ng
Replaces: ai-os-v10, ai-os-v11, ai-os-v12, ai-os-system
Conflicts: ai-os-v10, ai-os-v11, ai-os-v12, ai-os-system
Description: AI OS desktop coding assistant
 A local, offline-first coding assistant desktop app with model routing,
 editor execution loop, and optional remote heavy-model mode.
EOF

cat > "${PKG_DIR}/usr/bin/ai-os" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/opt/ai-os${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m ai_os.app "$@"
EOF

cat > "${PKG_DIR}/DEBIAN/postinst" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

OLD_LAUNCHER="/usr/local/bin/ai-os"
if [ -f "${OLD_LAUNCHER}" ] && grep -q "/opt/ai-os/app/main.py" "${OLD_LAUNCHER}"; then
  rm -f "${OLD_LAUNCHER}"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi

exit 0
EOF

cp "${ROOT_DIR}/packaging/ai-os.desktop" "${PKG_DIR}/usr/share/applications/ai-os.desktop"

find "${PKG_DIR}" -type d -exec chmod 0755 {} \;
find "${PKG_DIR}" -type f -exec chmod 0644 {} \;
chmod 0755 "${PKG_DIR}/usr/bin/ai-os"
chmod 0755 "${PKG_DIR}/DEBIAN/postinst"

dpkg-deb --build --root-owner-group "${PKG_DIR}" "${OUT_DEB}"

echo "Built package: ${OUT_DEB}"
