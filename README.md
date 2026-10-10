ĐĂNG TIKTOK ĐA TÀI KHOẢN

CHẠY TRÊN MÁY CÁ NHÂN
1. Cài Python 3.9+. Windows: bấm đúp start.bat. Mac/Linux: ./start.sh
2. Mở http://localhost:5000, bấm "Cài đặt" và nhập Client key, Client secret, Redirect URI.
3. "Thêm tài khoản" cho từng tài khoản, rồi chọn video + tài khoản và "Thêm vào hàng đợi".

ĐƯA LÊN MẠNG ĐỂ NỘP APP REVIEW CỦA TIKTOK
1. BẮT BUỘC đặt mật khẩu trước khi mở ra internet (APP_PASSWORD, xem start.bat). Không có mật khẩu thì ai biết địa chỉ cũng dùng được app của bạn.
2. Cho một tên miền https trỏ về cổng 5000 (VPS, Cloudflare Tunnel hoặc ngrok).
3. Khi APP_PASSWORD được đặt:
   - /          trang giới thiệu công khai (Website URL)
   - /terms     Terms of Service URL
   - /privacy   Privacy Policy URL
   - /callback  Redirect URI
   - /app       dashboard (cần đăng nhập, user: admin)
4. Trong Cài đặt, điền "Tên app" và "Email liên hệ" để hai trang pháp lý hiện đúng thông tin.
5. Nếu TikTok xác minh URL bằng file tiktokXXXX.txt, đặt file vào thư mục verify/.

Dữ liệu (token, hàng đợi, video) nằm trong thư mục data/. Không chia sẻ thư mục này.

CHẠY TRÊN RENDER (GÓI MIỄN PHÍ)
- Gói miễn phí: dịch vụ ngủ sau 15 phút không có truy cập, ổ đĩa bị xóa khi ngủ/khởi động lại.
  Vì vậy cần: (1) DATABASE_URL trỏ tới Postgres ngoài để giữ token, hàng đợi, cài đặt;
  (2) một dịch vụ ping /healthz mỗi 5 phút (ví dụ UptimeRobot) để dịch vụ không ngủ.
- Video tải lên vẫn nằm trên ổ đĩa tạm, nên bài hẹn giờ xa có thể mất video nếu dịch vụ khởi động lại.
- Đẩy thư mục này lên GitHub (không có data/), rồi trên Render: New > Blueprint > chọn repo.
  render.yaml sẽ hỏi các biến môi trường. Redirect URI tự suy ra từ địa chỉ Render (…/callback).
- File xác minh URL của TikTok (tiktokXXXX.txt) đặt vào thư mục verify/ rồi commit lên GitHub.
