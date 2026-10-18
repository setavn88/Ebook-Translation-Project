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

echo "Đang mở giao diện BookTranslator... (đóng cửa sổ này để tắt)"
python3 -m booktranslator giao-dien
