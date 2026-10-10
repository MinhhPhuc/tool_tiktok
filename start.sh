#!/usr/bin/env bash
cd "$(dirname "$0")"
# Khi đưa app lên mạng, đặt mật khẩu: export APP_PASSWORD='dat-mat-khau-dai-o-day'
python3 -m pip install -r requirements.txt && python3 app.py
