#!/bin/bash
# quant-practice Mac 一键搭建：不需要 Homebrew、不需要 Postgres.app。
# 用法：把本脚本、quant-practice-code_v2_2026-10-08.tar.gz、
#       quant_dump_v2_2026-10-08.sql.gz 放在同一目录（比如 ~/workspace/），终端里跑：
#       bash setup_mac.sh
# 已经手动解压过也行：把脚本拷进 quant-practice 文件夹里跑同样可以。
# 前提：机器上有 python3（没有的话先跑 xcode-select --install 装命令行工具，再重跑本脚本）。
set -e
cd "$(dirname "$0")"

if [ -f db.py ] && [ -f restore_quant.py ]; then
  : # 就在 quant-practice 目录里跑，直接往下走
else
  if [ ! -d quant-practice ]; then
    echo "解压代码包…"
    tar -xzf quant-practice-code_v2_2026-10-08.tar.gz
  fi
  cp quant_dump_v2_2026-10-08.sql.gz quant-practice/ 2>/dev/null || true
  cd quant-practice
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "没找到 python3。先在终端运行：xcode-select --install ，装完后重新运行本脚本。"
  exit 1
fi

echo "创建虚拟环境并安装依赖（首次约 3-5 分钟）…"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "还原数据库…"
./.venv/bin/python restore_quant.py

echo ""
echo "全部完成。以后使用："
echo "  cd \"$(pwd)\" && source .venv/bin/activate"
echo "  python db.py    # 冒烟测试，应打印 PostgreSQL 16.2 版本行"
