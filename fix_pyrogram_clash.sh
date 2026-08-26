#!/usr/bin/env bash
# Fix untuk error:
#   TypeError: Location.__init__() got an unexpected keyword argument 'client'
#
# Penyebab: paket "pyrogram" (klasik, sudah tidak di-maintain) dan
# "kurigram" (fork aktif) sama-sama menginstal modul bernama `pyrogram`
# di venv yang sama. Kalau keduanya pernah ke-install (mis. karena
# requirements lama pernah pakai pyrogram, lalu diganti kurigram tanpa
# uninstall dulu), file-file dari kedua paket bisa tercampur / versi lama
# tetap kepakai walau kurigram sudah "terinstal". Solusinya: bersihkan
# total lalu install ulang HANYA kurigram.
#
# Jalankan dari root project (folder yang berisi venv/), misal:
#   cd /root/aibot && bash fix_pyrogram_clash.sh

set -e

VENV_DIR="${1:-venv}"

if [ ! -d "$VENV_DIR" ]; then
  echo "Venv '$VENV_DIR' tidak ditemukan. Jalankan dari folder project, atau:"
  echo "  bash fix_pyrogram_clash.sh /path/ke/venv"
  exit 1
fi

source "$VENV_DIR/bin/activate"

echo "== Uninstall semua paket pyrogram/kurigram yang ada (bisa lebih dari satu) =="
pip uninstall -y pyrogram kurigram Pyrogram 2>/dev/null || true

echo "== Hapus sisa folder pyrogram di site-packages (kadang tersisa walau sudah uninstall) =="
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
rm -rf "$SITE_PACKAGES/pyrogram" "$SITE_PACKAGES"/pyrogram-*.dist-info "$SITE_PACKAGES"/Pyrogram-*.dist-info "$SITE_PACKAGES"/kurigram-*.dist-info 2>/dev/null || true

echo "== Bersihkan pycache lama project (opsional tapi disarankan) =="
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "== Install ulang dependency dari requirements.txt (kurigram==2.1.38, versi stabil) =="
pip install --no-cache-dir -r requirements.txt

echo "== Verifikasi hanya ada satu implementasi pyrogram (dari kurigram) =="
pip show kurigram
python -c "import pyrogram; print('pyrogram module path ->', pyrogram.__file__); print('version ->', pyrogram.__version__)"

echo ""
echo "Selesai. Sekarang restart bot-nya, misal:"
echo "  systemctl restart aibot   # kalau pakai systemd"
echo "  # atau jalankan manual: python main.py"
