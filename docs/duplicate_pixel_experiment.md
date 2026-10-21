# Thử nghiệm xác nhận ảnh nghi trùng bằng pixel

Mục tiêu: giảm số cặp dHash cần người xem mà không gộp nhầm hai ảnh khác nhau. Đây là thử nghiệm cho Pain Point A, không thay đổi quyết định của pipeline.

## Cơ sở chọn phương pháp

Meta mô tả [PDQ](https://github.com/facebook/ThreatExchange/blob/main/pdq/README.md) là perceptual hash để tìm ảnh gần giống; mã hash không tự chứng minh hai ảnh là cùng một tấm. Nghiên cứu [SSIM của Wang và cộng sự](https://www.cns.nyu.edu/~lcv/pubs/makeAbs.php?loc=Wang03) đưa ra phép đo cấu trúc giữa hai ảnh đã căn chỉnh. Vì cặp ứng viên của project đã có dHash, chúng tôi thử thêm hai điểm so pixel xám sau resize 256×256: normalized cross-correlation (NCC) và SSIM. Hai phép này rẻ hơn chạy SIFT/RANSAC, nhưng dễ bỏ sót ảnh crop hoặc xoay.

## Phương pháp đánh giá

- Dùng 100 cặp trong `near_duplicate_review_labels.jsonl` do một người gắn nhãn: 59 `duplicate`, 24 `similar_not_duplicate`, 17 `different`. Mẫu phân tầng theo khoảng cách dHash và cùng/khác entity, nên tỷ lệ duplicate trong mẫu **không phải** tỷ lệ của toàn corpus.
- Chia theo thành phần liên thông URL (seed `20261021`), tránh một URL xuất hiện ở cả calibration và test. Calibration có 40 cặp, test có 60 cặp.
- Trên calibration, chọn phép đo và ngưỡng bắt được nhiều cặp duplicate nhất với **0 cặp khác bị gắn cờ**. Nếu không có ngưỡng phù hợp, chọn không gắn cờ. Chạy một lần trên test, không chỉnh lại ngưỡng theo test.
- Lệnh tái lập: `.venv\Scripts\python.exe scripts\evaluate_duplicate_pixels.py`. JSON chi tiết ở `data/analysis/duplicate_pixels_report.json` (không commit vì các nguồn ảnh thuộc dữ liệu nội bộ).

## Kết quả đo tại máy

| Phần | Cặp | Cùng ảnh gắn đúng | Cặp khác gắn nhầm | Recall cùng ảnh | Precision cặp gắn cờ |
|---|---:|---:|---:|---:|---:|
| Calibration | 40 | 20 | 0 | 66,67% | 100% |
| Test | 60 | 15 | **1** | 51,72% | 93,75% |

Ngưỡng chọn từ calibration là NCC ≥0,8963. Trích đặc trưng 100 cặp mất 5,388 giây, tương đương 18,56 cặp/giây trên máy này; chưa tính dHash, tải ảnh hoặc người duyệt. Cặp test số 75 bị gắn nhầm: hai ảnh sản phẩm cùng loại áo, bố cục gần giống nhưng là hai ảnh riêng (khác phần mũ và khung hình). Điều này xác nhận **không được dùng ngưỡng này để tự động gộp/xóa**. Điểm NCC có thể dùng để xếp ưu tiên hàng người duyệt; muốn ước lượng precision ngoài thực tế cần mẫu xác nhận độc lập, có trọng số theo strata và nhiều người gắn nhãn hơn.

Baseline SIFT/RANSAC trước đó đạt 0 cặp gắn cờ trên test ở ràng buộc calibration 0 lỗi (xem báo cáo local `duplicate_geometry_report.json`). Thí nghiệm NCC thu hồi được một phần cặp cùng ảnh nhanh hơn, nhưng một lỗi gộp nhầm là quá rủi ro khi asset còn gắn với nhiều entity. SHA-256 trên cùng nội dung xử lý vẫn là quy tắc gộp tự động an toàn; dHash/NCC chỉ tạo hoặc ưu tiên ứng viên.
