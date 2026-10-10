@echo off
chcp 65001 >nul
rem Khi đưa app lên mạng (qua tên miền https), bỏ chữ "rem" ở dòng dưới và đặt mật khẩu của bạn:
rem set APP_PASSWORD=dat-mat-khau-dai-o-day
python -m pip install -r requirements.txt
python app.py
pause
