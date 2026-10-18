#!/bin/bash
cd "$(dirname "$0")"
# Chạy được cả khi chưa "pip install": lấy mã nguồn trực tiếp từ thư mục src
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v python3 >/dev/null; then
  echo "[LỖI] Chưa cài Python. Tải tại https://www.python.org/downloads/ rồi chạy lại."
  read -r -p "Nhấn Enter để đóng..."
  exit 1
fi

if ! python3 -c "import anthropic, openai" 2>/dev/null; then
  echo "Lần đầu chạy: đang cài các thư viện cần thiết, vui lòng chờ 1-2 phút..."
  python3 -m pip install -e ".[openai]" || { echo "[LỖI] Cài đặt thất bại."; read -r -p "Nhấn Enter để đóng..."; exit 1; }
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude >/dev/null; then
  echo
  echo "Chưa có Claude Code trên máy. Claude Code giúp dịch bằng gói Claude Pro/Max mà không tốn tiền API."
  read -r -p "Cài Claude Code ngay bây giờ? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    curl -fsSL https://claude.ai/install.sh | bash
    if command -v claude >/dev/null; then
      echo "Đăng nhập Claude Code: làm theo hướng dẫn, đăng nhập xong gõ /exit."
      claude
    else
      echo "[CẢNH BÁO] Chưa tìm thấy lệnh claude sau khi cài. Xem tab \"Kết nối AI\" trong giao diện."
    fi
  fi
fi

echo "Đang mở giao diện BookTranslator... (đóng cửa sổ này để tắt)"
python3 -m booktranslator giao-dien
